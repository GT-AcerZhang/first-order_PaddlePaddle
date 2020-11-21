import numpy as np
import paddle
import paddle.nn as nn
import paddle.nn.functional as F
from modules.util import Hourglass, AntiAliasInterpolation2d, kp2gaussian, \
    make_coordinate_grid


class DenseMotionNetwork(nn.Layer):
    """
    Module that predicting a dense motion from sparse motion representation given by kp_source and kp_driving
    """
    
    def __init__(self, block_expansion, num_blocks, max_features, num_kp, num_channels, estimate_occlusion_map=False,
                 scale_factor=1, kp_variance=0.01, **kwargs):
        super(DenseMotionNetwork, self).__init__(**kwargs)
        self.hourglass = Hourglass(block_expansion=block_expansion, in_features=(num_kp + 1) * (num_channels + 1),
                                   max_features=max_features, num_blocks=num_blocks)
        
        self.mask = nn.Conv2D(self.hourglass.out_filters, num_kp + 1, kernel_size=(7, 7), padding=(3, 3))
        
        if estimate_occlusion_map:
            self.occlusion = nn.Conv2D(self.hourglass.out_filters, 1, kernel_size=(7, 7), padding=(3, 3))
        else:
            self.occlusion = None
        
        self.num_kp = num_kp
        self.scale_factor = scale_factor
        self.kp_variance = kp_variance
        
        if self.scale_factor != 1:
            self.down = AntiAliasInterpolation2d(num_channels, self.scale_factor)
    
    def create_heatmap_representations(self, source_image, kp_driving, kp_source):
        """
        Eq 6. in the paper H_k(z)
        """
        spatial_size = source_image.shape[2:]
        gaussian_driving = kp2gaussian(kp_driving, spatial_size=spatial_size, kp_variance=self.kp_variance)
        gaussian_source = kp2gaussian(kp_source, spatial_size=spatial_size, kp_variance=self.kp_variance)
        heatmap = gaussian_driving - gaussian_source
        
        # adding background feature
        zeros = paddle.to_tensor(np.zeros((heatmap.shape[0], 1, spatial_size[0], spatial_size[1]), dtype=np.float32))
        heatmap = paddle.concat([zeros, heatmap], axis=1)
        heatmap = heatmap.unsqueeze(2)
        return heatmap
    
    def create_sparse_motions(self, source_image, kp_driving, kp_source):
        """
        Eq 4. in the paper T_{s<-d}(z)
        """
        bs, _, h, w = source_image.shape
        identity_grid = make_coordinate_grid((h, w))
        identity_grid = identity_grid.reshape((1, 1, h, w, 2))
        coordinate_grid = identity_grid - kp_driving['value'].reshape((bs, self.num_kp, 1, 1, 2))
        if 'jacobian' in kp_driving:
            jacobian = paddle.matmul(kp_source['jacobian'], paddle.inverse(kp_driving['jacobian']))
            dim_1, dim_2, *else_dim = jacobian.shape
            jacobian = jacobian.reshape((-1, *else_dim))
            jacobian = jacobian.unsqueeze(-3).unsqueeze(-3)
            jacobian = jacobian.tile((1, h, w, 1, 1))
            
            _, _, *dimm = coordinate_grid.shape
            coordinate_grid = coordinate_grid.reshape((-1, *dimm))
            coordinate_grid = paddle.matmul(jacobian, coordinate_grid.unsqueeze(-1))
            coordinate_grid = coordinate_grid.squeeze(-1)
            coordinate_grid = coordinate_grid.reshape((dim_1, dim_2, *(coordinate_grid.shape[1:])))
        
        driving_to_source = coordinate_grid + kp_source['value'].reshape((bs, self.num_kp, 1, 1, 2))
        
        # adding background feature
        identity_grid = identity_grid.tile((bs, 1, 1, 1, 1))
        sparse_motions = paddle.concat([identity_grid, driving_to_source], axis=1)
        return sparse_motions
    
    def create_deformed_source_image(self, source_image, sparse_motions):
        """
        Eq 7. in the paper \hat{T}_{s<-d}(z)
        """
        bs, _, h, w = source_image.shape
        source_image = source_image.reshape((-1, h, w))
        source_repeat = paddle.tile(source_image.unsqueeze(1).unsqueeze(1), (self.num_kp + 1, 1, 1, 1, 1))
        source_repeat = source_repeat.reshape((bs * (self.num_kp + 1), -1, h, w))
        sparse_motions = sparse_motions.reshape((bs * (self.num_kp + 1), h, w, -1))
        # Important: grid_sampler(..., align_corners=True) in pytorch 1.0
        sparse_deformed = F.grid_sample(source_repeat, sparse_motions, mode='bilinear', padding_mode='zeros', align_corners=True)
        sparse_deformed = sparse_deformed.reshape((bs, self.num_kp + 1, -1, h, w))
        return sparse_deformed
    
    def forward(self, source_image, kp_driving, kp_source):
        if self.scale_factor != 1:
            source_image = self.down(source_image)
        
        bs, _, h, w = source_image.shape
        out_dict = dict()
        heatmap_representation = self.create_heatmap_representations(source_image, kp_driving, kp_source)
        sparse_motion = self.create_sparse_motions(source_image, kp_driving, kp_source)
        deformed_source = self.create_deformed_source_image(source_image, sparse_motion)
        out_dict['sparse_deformed'] = deformed_source
        
        buf = paddle.concat([heatmap_representation, deformed_source], axis=2)
        buf = buf.reshape((bs, -1, h, w))
        prediction = self.hourglass(buf)
        
        mask = self.mask(prediction)
        mask = F.softmax(mask, axis=1)
        out_dict['mask'] = mask
        mask = mask.unsqueeze(2)
        sparse_motion = paddle.transpose(sparse_motion, (0, 1, 4, 2, 3))
        deformation = paddle.sum(sparse_motion * mask, axis=1)
        deformation = paddle.transpose(deformation, (0, 2, 3, 1))
        out_dict['deformation'] = deformation
        
        # Sec. 3.2 in the paper
        if self.occlusion:
            occlusion_map = F.sigmoid(self.occlusion(prediction))
            out_dict['occlusion_map'] = occlusion_map
        return out_dict
