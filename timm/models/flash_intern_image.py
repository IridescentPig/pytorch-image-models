"""Flash Intern Image
A Pytorch Implementation of Flash Intern Image as decribed in:

`InternImage: Exploring Large-Scale Vision Foundation Models with Deformable Convolutions`
    - https://arxiv.org/pdf/2103.14030

`DCNv4`
    - https://arxiv.org/pdf/2401.06197

Code/weights from https://github.com/OpenGVLab/DCNv4, original copyright/license info below
"""
# --------------------------------------------------------
# Flash Intern Image
# Copyright (c) 2024 OpenGVLab
# Licensed under The MIT License [see LICENSE for details]
# --------------------------------------------------------
import torch
import torch.nn as nn
from torch.nn.init import xavier_uniform_, constant_
from collections import OrderedDict
import torch.utils.checkpoint as checkpoint
from timm.models.layers import trunc_normal_, DropPath
from timm.data import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from ._registry import register_model, generate_default_cfgs
from ._builder import build_model_with_cfg
import torch.nn.functional as F
from ._manipulate import checkpoint_seq
from typing import Dict, Any
import warnings
import logging

__all__ = ['FlashInternImage']

_logger = logging.getLogger(__name__)

dcn_version = 'DCNv4'
try:
    import DCNv4
except ImportError:
    dcn_version = 'DCNv3'
    

class to_channels_first(nn.Module):

    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x.permute(0, 3, 1, 2)


class to_channels_last(nn.Module):

    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x.permute(0, 2, 3, 1)
    

def build_norm_layer(dim,
                     norm_layer,
                     in_format='channels_last',
                     out_format='channels_last',
                     eps=1e-6):
    layers = []
    if norm_layer == 'BN':
        if in_format == 'channels_last':
            layers.append(to_channels_first())
        layers.append(nn.BatchNorm2d(dim))
        if out_format == 'channels_last':
            layers.append(to_channels_last())
    elif norm_layer == 'LN':
        if in_format == 'channels_first':
            layers.append(to_channels_last())
        layers.append(nn.LayerNorm(dim, eps=eps))
        if out_format == 'channels_first':
            layers.append(to_channels_first())
    else:
        raise NotImplementedError(
            f'build_norm_layer does not support {norm_layer}')
    return nn.Sequential(*layers)


def build_act_layer(act_layer):
    if act_layer == 'ReLU':
        return nn.ReLU(inplace=True)
    elif act_layer == 'SiLU':
        return nn.SiLU(inplace=True)
    elif act_layer == 'GELU':
        return nn.GELU()

    raise NotImplementedError(f'build_act_layer does not support {act_layer}')


def _get_reference_points(spatial_shapes, device, kernel_h, kernel_w, dilation_h, dilation_w, pad_h=0, pad_w=0, stride_h=1, stride_w=1):
    _, H_, W_, _ = spatial_shapes
    H_out = (H_ - (dilation_h * (kernel_h - 1) + 1)) // stride_h + 1
    W_out = (W_ - (dilation_w * (kernel_w - 1) + 1)) // stride_w + 1

    ref_y, ref_x = torch.meshgrid(
        torch.linspace(
            # pad_h + 0.5,
            # H_ - pad_h - 0.5,
            (dilation_h * (kernel_h - 1)) // 2 + 0.5,
            (dilation_h * (kernel_h - 1)) // 2 + 0.5 + (H_out - 1) * stride_h,
            H_out,
            dtype=torch.float32,
            device=device),
        torch.linspace(
            # pad_w + 0.5,
            # W_ - pad_w - 0.5,
            (dilation_w * (kernel_w - 1)) // 2 + 0.5,
            (dilation_w * (kernel_w - 1)) // 2 + 0.5 + (W_out - 1) * stride_w,
            W_out,
            dtype=torch.float32,
            device=device))
    ref_y = ref_y.reshape(-1)[None] / H_
    ref_x = ref_x.reshape(-1)[None] / W_

    ref = torch.stack((ref_x, ref_y), -1).reshape(
        1, H_out, W_out, 1, 2)

    return ref


def _generate_dilation_grids(spatial_shapes, kernel_h, kernel_w, dilation_h, dilation_w, group, device):
    _, H_, W_, _ = spatial_shapes
    points_list = []
    x, y = torch.meshgrid(
        torch.linspace(
            -((dilation_w * (kernel_w - 1)) // 2),
            -((dilation_w * (kernel_w - 1)) // 2) +
            (kernel_w - 1) * dilation_w, kernel_w,
            dtype=torch.float32,
            device=device),
        torch.linspace(
            -((dilation_h * (kernel_h - 1)) // 2),
            -((dilation_h * (kernel_h - 1)) // 2) +
            (kernel_h - 1) * dilation_h, kernel_h,
            dtype=torch.float32,
            device=device))

    points_list.extend([x / W_, y / H_])
    grid = torch.stack(points_list, -1).reshape(-1, 1, 2).\
        repeat(1, group, 1).permute(1, 0, 2)
    grid = grid.reshape(1, 1, 1, group * kernel_h * kernel_w, 2)

    return grid


def dcnv3_core_pytorch(
        input, offset, mask, kernel_h,
        kernel_w, stride_h, stride_w, pad_h,
        pad_w, dilation_h, dilation_w, group,
        group_channels, offset_scale):
    # for debug and test only,
    # need to use cuda version instead
    input = F.pad(
        input,
        [0, 0, pad_h, pad_h, pad_w, pad_w])
    N_, H_in, W_in, _ = input.shape
    _, H_out, W_out, _ = offset.shape

    ref = _get_reference_points(
        input.shape, input.device, kernel_h, kernel_w, dilation_h, dilation_w, pad_h, pad_w, stride_h, stride_w)
    grid = _generate_dilation_grids(
        input.shape, kernel_h, kernel_w, dilation_h, dilation_w, group, input.device)
    spatial_norm = torch.tensor([W_in, H_in]).reshape(1, 1, 1, 2).\
        repeat(1, 1, 1, group*kernel_h*kernel_w).to(input.device)

    sampling_locations = (ref + grid * offset_scale).repeat(N_, 1, 1, 1, 1).flatten(3, 4) + \
        offset * offset_scale / spatial_norm

    P_ = kernel_h * kernel_w
    sampling_grids = 2 * sampling_locations - 1
    # N_, H_in, W_in, group*group_channels -> N_, H_in*W_in, group*group_channels -> N_, group*group_channels, H_in*W_in -> N_*group, group_channels, H_in, W_in
    input_ = input.view(N_, H_in*W_in, group*group_channels).transpose(1, 2).\
        reshape(N_*group, group_channels, H_in, W_in)
    # N_, H_out, W_out, group*P_*2 -> N_, H_out*W_out, group, P_, 2 -> N_, group, H_out*W_out, P_, 2 -> N_*group, H_out*W_out, P_, 2
    sampling_grid_ = sampling_grids.view(N_, H_out*W_out, group, P_, 2).transpose(1, 2).\
        flatten(0, 1)
    # N_*group, group_channels, H_out*W_out, P_
    sampling_input_ = F.grid_sample(
        input_, sampling_grid_, mode='bilinear', padding_mode='zeros', align_corners=False)

    # (N_, H_out, W_out, group*P_) -> N_, H_out*W_out, group, P_ -> (N_, group, H_out*W_out, P_) -> (N_*group, 1, H_out*W_out, P_)
    mask = mask.view(N_, H_out*W_out, group, P_).transpose(1, 2).\
        reshape(N_*group, 1, H_out*W_out, P_)
    output = (sampling_input_ * mask).sum(-1).view(N_,
                                                   group*group_channels, H_out*W_out)

    return output.transpose(1, 2).reshape(N_, H_out, W_out, -1).contiguous()


def _is_power_of_2(n):
    if (not isinstance(n, int)) or (n < 0):
        raise ValueError(
            "invalid input for _is_power_of_2: {} (type: {})".format(n, type(n)))

    return (n & (n - 1) == 0) and n != 0


class CenterFeatureScaleModule(nn.Module):
    def forward(self,
                query,
                center_feature_scale_proj_weight,
                center_feature_scale_proj_bias):
        center_feature_scale = F.linear(query,
                                        weight=center_feature_scale_proj_weight,
                                        bias=center_feature_scale_proj_bias).sigmoid()
        return center_feature_scale
    

class DCNv3_pytorch(nn.Module):
    def __init__(
            self,
            channels=64,
            kernel_size=3,
            dw_kernel_size=None,
            stride=1,
            pad=1,
            dilation=1,
            group=4,
            offset_scale=1.0,
            act_layer='GELU',
            norm_layer='LN',
            center_feature_scale=False):
        """
        DCNv3 Module
        :param channels
        :param kernel_size
        :param stride
        :param pad
        :param dilation
        :param group
        :param offset_scale
        :param act_layer
        :param norm_layer
        """
        super().__init__()
        if channels % group != 0:
            raise ValueError(
                f'channels must be divisible by group, but got {channels} and {group}')
        _d_per_group = channels // group
        dw_kernel_size = dw_kernel_size if dw_kernel_size is not None else kernel_size
        # you'd better set _d_per_group to a power of 2 which is more efficient in our CUDA implementation
        if not _is_power_of_2(_d_per_group):
            warnings.warn(
                "You'd better set channels in DCNv3 to make the dimension of each attention head a power of 2 "
                "which is more efficient in our CUDA implementation.")

        self.offset_scale = offset_scale
        self.channels = channels
        self.kernel_size = kernel_size
        self.dw_kernel_size = dw_kernel_size
        self.stride = stride
        self.dilation = dilation
        self.pad = pad
        self.group = group
        self.group_channels = channels // group
        self.offset_scale = offset_scale
        self.center_feature_scale = center_feature_scale

        self.dw_conv = nn.Sequential(
            nn.Conv2d(
                channels,
                channels,
                kernel_size=dw_kernel_size,
                stride=1,
                padding=(dw_kernel_size - 1) // 2,
                groups=channels),
            build_norm_layer(
                channels,
                norm_layer,
                'channels_first',
                'channels_last'),
            build_act_layer(act_layer))
        self.offset = nn.Linear(
            channels,
            group * kernel_size * kernel_size * 2)
        self.mask = nn.Linear(
            channels,
            group * kernel_size * kernel_size)
        self.input_proj = nn.Linear(channels, channels)
        self.output_proj = nn.Linear(channels, channels)
        self._reset_parameters()
        
        if center_feature_scale:
            self.center_feature_scale_proj_weight = nn.Parameter(
                torch.zeros((group, channels), dtype=torch.float))
            self.center_feature_scale_proj_bias = nn.Parameter(
                torch.tensor(0.0, dtype=torch.float).view((1,)).repeat(group, ))
            self.center_feature_scale_module = CenterFeatureScaleModule()

    def _reset_parameters(self):
        constant_(self.offset.weight.data, 0.)
        constant_(self.offset.bias.data, 0.)
        constant_(self.mask.weight.data, 0.)
        constant_(self.mask.bias.data, 0.)
        xavier_uniform_(self.input_proj.weight.data)
        constant_(self.input_proj.bias.data, 0.)
        xavier_uniform_(self.output_proj.weight.data)
        constant_(self.output_proj.bias.data, 0.)

    def forward(self, input, shape=None):
        """
        :param query                       (N, H, W, C)
        :return output                     (N, H, W, C)
        """
        # N, H, W, _ = input.shape
        N, L, C = input.shape
        if shape is not None:
            H, W = shape
        else:
            H, W = int(L**0.5), int(L**0.5)

        x = input.reshape(N, H, W, -1)
        x = self.input_proj(x)
        x_proj = x

        x1 = input.reshape(N, H, W, -1).permute(0, 3, 1, 2)
        x1 = self.dw_conv(x1)
        offset = self.offset(x1)
        mask = self.mask(x1).reshape(N, H, W, self.group, -1)
        mask = F.softmax(mask, -1).reshape(N, H, W, -1)

        x = dcnv3_core_pytorch(
            x, offset, mask,
            self.kernel_size, self.kernel_size,
            self.stride, self.stride,
            self.pad, self.pad,
            self.dilation, self.dilation,
            self.group, self.group_channels,
            self.offset_scale)
        if self.center_feature_scale:
            center_feature_scale = self.center_feature_scale_module(
                x1, self.center_feature_scale_proj_weight, self.center_feature_scale_proj_bias)
            # N, H, W, groups -> N, H, W, groups, 1 -> N, H, W, groups, _d_per_group -> N, H, W, channels
            center_feature_scale = center_feature_scale[..., None].repeat(
                1, 1, 1, 1, self.channels // self.group).flatten(-2)
            x = x * (1 - center_feature_scale) + x_proj * center_feature_scale
        x = self.output_proj(x)
        x = x.reshape(N, L, -1)
        return x
    
# --- DCNv3 pure pytorch implementation finished --- #
# --- FlashInternImage implementation start --- #
class CrossAttention(nn.Module):
    r""" Cross Attention Module
    Args:
        dim (int): Number of input channels.
        num_heads (int): Number of attention heads. Default: 8
        qkv_bias (bool, optional):  If True, add a learnable bias to q, k, v.
            Default: False.
        qk_scale (float | None, optional): Override default qk scale of
            head_dim ** -0.5 if set. Default: None.
        attn_drop (float, optional): Dropout ratio of attention weight.
            Default: 0.0
        proj_drop (float, optional): Dropout ratio of output. Default: 0.0
        attn_head_dim (int, optional): Dimension of attention head.
        out_dim (int, optional): Dimension of output.
    """

    def __init__(self,
                 dim,
                 num_heads=8,
                 qkv_bias=False,
                 qk_scale=None,
                 attn_drop=0.,
                 proj_drop=0.,
                 attn_head_dim=None,
                 out_dim=None):
        super().__init__()
        if out_dim is None:
            out_dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        if attn_head_dim is not None:
            head_dim = attn_head_dim
        all_head_dim = head_dim * self.num_heads
        self.scale = qk_scale or head_dim ** -0.5
        assert all_head_dim == dim

        self.q = nn.Linear(dim, all_head_dim, bias=False)
        self.k = nn.Linear(dim, all_head_dim, bias=False)
        self.v = nn.Linear(dim, all_head_dim, bias=False)

        if qkv_bias:
            self.q_bias = nn.Parameter(torch.zeros(all_head_dim))
            self.k_bias = nn.Parameter(torch.zeros(all_head_dim))
            self.v_bias = nn.Parameter(torch.zeros(all_head_dim))
        else:
            self.q_bias = None
            self.k_bias = None
            self.v_bias = None

        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(all_head_dim, out_dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x, k=None, v=None):
        B, N, C = x.shape
        N_k = k.shape[1]
        N_v = v.shape[1]

        q_bias, k_bias, v_bias = None, None, None
        if self.q_bias is not None:
            q_bias = self.q_bias
            k_bias = self.k_bias
            v_bias = self.v_bias

        q = F.linear(input=x, weight=self.q.weight, bias=q_bias)
        q = q.reshape(B, N, 1, self.num_heads,
                      -1).permute(2, 0, 3, 1,
                                  4).squeeze(0)  # (B, N_head, N_q, dim)

        k = F.linear(input=k, weight=self.k.weight, bias=k_bias)
        k = k.reshape(B, N_k, 1, self.num_heads, -1).permute(2, 0, 3, 1,
                                                             4).squeeze(0)

        v = F.linear(input=v, weight=self.v.weight, bias=v_bias)
        v = v.reshape(B, N_v, 1, self.num_heads, -1).permute(2, 0, 3, 1,
                                                             4).squeeze(0)

        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))  # (B, N_head, N_q, N_k)

        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, -1)
        x = self.proj(x)
        x = self.proj_drop(x)

        return x
    

class AttentiveBlock(nn.Module):
    r"""Attentive Block
    Args:
        dim (int): Number of input channels.
        num_heads (int): Number of attention heads. Default: 8
        qkv_bias (bool, optional):  If True, add a learnable bias to q, k, v.
            Default: False.
        qk_scale (float | None, optional): Override default qk scale of
            head_dim ** -0.5 if set. Default: None.
        drop (float, optional): Dropout rate. Default: 0.0.
        attn_drop (float, optional): Attention dropout rate. Default: 0.0.
        drop_path (float | tuple[float], optional): Stochastic depth rate.
            Default: 0.0.
        norm_layer (nn.Module, optional): Normalization layer.  Default: nn.LayerNorm.
        attn_head_dim (int, optional): Dimension of attention head. Default: None.
        out_dim (int, optional): Dimension of output. Default: None.
    """

    def __init__(self,
                 dim,
                 num_heads,
                 qkv_bias=False,
                 qk_scale=None,
                 drop=0.,
                 attn_drop=0.,
                 drop_path=0.,
                 norm_layer="LN",
                 attn_head_dim=None,
                 out_dim=None):
        super().__init__()

        self.norm1_q = build_norm_layer(dim, norm_layer, eps=1e-6)
        self.norm1_k = build_norm_layer(dim, norm_layer, eps=1e-6)
        self.norm1_v = build_norm_layer(dim, norm_layer, eps=1e-6)
        self.cross_dcn = CrossAttention(dim,
                                        num_heads=num_heads,
                                        qkv_bias=qkv_bias,
                                        qk_scale=qk_scale,
                                        attn_drop=attn_drop,
                                        proj_drop=drop,
                                        attn_head_dim=attn_head_dim,
                                        out_dim=out_dim)

        self.drop_path = DropPath(
            drop_path) if drop_path > 0. else nn.Identity()

    def forward(self,
                x_q,
                x_kv,
                pos_q,
                pos_k,
                bool_masked_pos,
                rel_pos_bias=None):
        x_q = self.norm1_q(x_q + pos_q)
        x_k = self.norm1_k(x_kv + pos_k)
        x_v = self.norm1_v(x_kv)

        x = self.cross_dcn(x_q, k=x_k, v=x_v)

        return x


class AttentionPoolingBlock(AttentiveBlock):

    def forward(self, x):
        x_q = x.mean(1, keepdim=True)
        x_kv = x
        pos_q, pos_k = 0, 0
        x = super().forward(x_q, x_kv, pos_q, pos_k,
                            bool_masked_pos=None,
                            rel_pos_bias=None)
        x = x.squeeze(1)
        return x
    

class StemLayer(nn.Module):
    r""" Stem layer of InternImage
    Args:
        in_chans (int): number of input channels
        out_chans (int): number of output channels
        act_layer (str): activation layer
        norm_layer (str): normalization layer
    """

    def __init__(self,
                 in_chans=3,
                 out_chans=96,
                 act_layer='GELU',
                 norm_layer='BN'):
        super().__init__()
        self.conv1 = nn.Conv2d(in_chans,
                               out_chans // 2,
                               kernel_size=3,
                               stride=2,
                               padding=1)
        self.norm1 = build_norm_layer(out_chans // 2, norm_layer,
                                      'channels_first', 'channels_first')
        self.act = build_act_layer(act_layer)
        self.conv2 = nn.Conv2d(out_chans // 2,
                               out_chans,
                               kernel_size=3,
                               stride=2,
                               padding=1)
        self.norm2 = build_norm_layer(out_chans, norm_layer, 'channels_first',
                                      'channels_last')

    def forward(self, x):
        x = self.conv1(x)
        x = self.norm1(x)
        x = self.act(x)
        x = self.conv2(x)
        x = self.norm2(x)
        return x


class DownsampleLayer(nn.Module):
    r""" Downsample layer of InternImage
    Args:
        channels (int): number of input channels
        norm_layer (str): normalization layer
    """

    def __init__(self, channels, norm_layer='LN'):
        super().__init__()
        self.conv = nn.Conv2d(channels,
                              2 * channels,
                              kernel_size=3,
                              stride=2,
                              padding=1,
                              bias=False)
        self.norm = build_norm_layer(2 * channels, norm_layer,
                                     'channels_first', 'channels_first')


    def forward(self, x, shape=None):
        H, W = shape
        N, HW, C = x.shape
        x = x.view(N, H, W, C)
        x = self.conv(x.permute(0, 3, 1, 2))
        x = self.norm(x) # B C H W
        H, W = x.size(2), x.size(3)
        x = x.flatten(2).permute(0, 2, 1)

        return x, (H, W)
    

class MLPLayer(nn.Module):
    r""" MLP layer of InternImage
    Args:
        in_features (int): number of input features
        hidden_features (int): number of hidden features
        out_features (int): number of output features
        act_layer (str): activation layer
        drop (float): dropout rate
    """

    def __init__(self,
                 in_features,
                 hidden_features=None,
                 out_features=None,
                 act_layer='GELU',
                 mlp_fc2_bias=False,
                 drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features, bias=True)
        self.act = build_act_layer(act_layer)
        self.fc2 = nn.Linear(hidden_features, out_features, bias=mlp_fc2_bias)
        self.drop = nn.Dropout(drop)


    def forward(self, x, shape, level_idx=0):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x
    

class InternImageLayer(nn.Module):
    r""" Basic layer of InternImage
    Args:
        core_op (str): core operation of InternImage
        channels (int): number of input channels
        groups (int): Groups of each block.
        mlp_ratio (float): ratio of mlp hidden features to input channels, Default: 4.
        drop (float): dropout rate, Default: 0.
        drop_path (float): drop path rate, Default: 0.
        act_layer (str): activation layer, Default: 'GELU'.
        norm_layer (str): normalization layer, Default: 'LN'.
        post_norm (bool): whether to use post normalization, Default: False.
        layer_scale (float): layer scale, Default: None.
        offset_scale (float): offset scale, Default: 1.0.
        with_cp (bool): whether to use checkpoint, Default: False.
        dcn_output_bias (bool): whether to use dcn output bias, Default: False.
        mlp_fc2_bias (bool): whether to use mlp fc2 bias, Default: False.
        dw_kernel_size (int): Size of the dwconv, Default: None.
        res_post_norm (bool): whether to use res post normalization, Default: False.
        center_feature_scale (bool): whether to use center feature scale, Default: False.
    """

    def __init__(self,
                 core_op,
                 channels,
                 groups,
                 mlp_ratio=4.,
                 drop=0.,
                 drop_path=0.,
                 act_layer='GELU',
                 norm_layer='LN',
                 post_norm=False,
                 layer_scale=None,
                 offset_scale=1.0,
                 with_cp=False,
                 dcn_output_bias=False,
                 mlp_fc2_bias=False,
                 dw_kernel_size=None, # for InternImage-H/G
                 res_post_norm=False, # for InternImage-H/G
                 center_feature_scale=False): # for InternImage-H/G
        super().__init__()
        self.channels = channels
        self.groups = groups
        self.mlp_ratio = mlp_ratio
        self.with_cp = with_cp

        self.norm1 = build_norm_layer(channels, 'LN')
        self.post_norm = post_norm
        if dcn_version == 'DCNv4' and core_op == 'DCNv4':
            self.dcn = DCNv4.DCNv4(
                channels=channels,
                group=groups,
                offset_scale=offset_scale,
                dw_kernel_size=dw_kernel_size,
                output_bias=dcn_output_bias,
            )
        else:
            self.dcn = DCNv3_pytorch(
                channels=channels,
                group=groups,
                offset_scale=offset_scale,
                dw_kernel_size=dw_kernel_size,
                center_feature_scale=center_feature_scale
            )
        
        self.drop_path = DropPath(drop_path) if drop_path > 0. \
            else nn.Identity()
        self.norm2 = build_norm_layer(channels, 'LN')
        self.mlp = MLPLayer(in_features=channels,
                            hidden_features=int(channels * mlp_ratio),
                            act_layer=act_layer,
                            drop=drop,
                            mlp_fc2_bias=mlp_fc2_bias
                            )
        self.layer_scale = layer_scale is not None
        if self.layer_scale:
            self.gamma1 = nn.Parameter(layer_scale * torch.ones(channels),
                                       requires_grad=True)
            self.gamma2 = nn.Parameter(layer_scale * torch.ones(channels),
                                       requires_grad=True)
        self.res_post_norm = res_post_norm
        if res_post_norm:
            self.res_post_norm1 = build_norm_layer(channels, 'LN')
            self.res_post_norm2 = build_norm_layer(channels, 'LN')
    def forward(self, x, shape, level_idx=0):

        def _inner_forward(x, shape, level_idx):
            if not self.layer_scale:
                if self.post_norm:
                    x = x + self.drop_path(self.norm1(self.dcn(x, shape)))
                    x = x + self.drop_path(self.norm2(self.mlp(x, shape, level_idx)))
                elif self.res_post_norm: # for InternImage-H/G
                    x = x + self.drop_path(self.res_post_norm1(self.dcn(self.norm1(x), shape)))
                    x = x + self.drop_path(self.res_post_norm2(self.mlp(self.norm2(x), shape, level_idx)))

                else:
                    x = x + self.drop_path(self.dcn(self.norm1(x), shape))
                    x = x + self.drop_path(self.mlp(self.norm2(x), shape, level_idx))
                return x
            if self.post_norm:
                x = x + self.drop_path(self.gamma1 * self.norm1(self.dcn(x, shape)))
                x = x + self.drop_path(self.gamma2 * self.norm2(self.mlp(x, shape, level_idx)))
            else:
                x = x + self.drop_path(self.gamma1 * self.dcn(self.norm1(x), shape))
                x = x + self.drop_path(self.gamma2 * self.mlp(self.norm2(x), shape, level_idx))
            return x

        if self.with_cp and x.requires_grad:
            x = checkpoint.checkpoint(_inner_forward, x, shape, level_idx)
        else:
            x = _inner_forward(x, shape, level_idx)

        return x


class InternImageBlock(nn.Module):
    r""" Block of InternImage
    Args:
        core_op (str): core operation of InternImage
        channels (int): number of input channels
        depth (int): Depth of each block.
        groups (int): Groups of each block.
        downsample (bool): Whether to use downsample, Default: True.
        downsample_layer (nn.Module): Downsample layer, Default: DownsampleLayer.
        mlp_ratio (float): ratio of mlp hidden features to input channels, Default: 4.
        drop (float): dropout rate, Default: 0.
        drop_path (float): drop path rate, Default: 0.
        act_layer (str): activation layer, Default: 'GELU'.
        norm_layer (str): normalization layer, Default: 'LN'.
        post_norm (bool): whether to use post normalization, Default: False.
        offset_scale (float): offset scale, Default: 0.5.
        layer_scale (float): layer scale, Default: None.
        with_cp (bool): whether to use checkpoint, Default: False.
        dcn_output_bias (bool): whether to use dcn output bias, Default: False.
        mlp_fc2_bias (bool): whether to use mlp fc2 bias, Default: False.
        dw_kernel_size (int): Size of the dwconv, Default: None.
        post_norm_block_ids (list): block ids for post normalization, Default: None.
        res_post_norm (bool): whether to use res post normalization, Default: False.
        center_feature_scale (bool): whether to use center feature scale, Default: False.
    """

    def __init__(self,
                 core_op,
                 channels,
                 depth,
                 groups,
                 downsample=True,
                 downsample_layer=DownsampleLayer,
                 mlp_ratio=4.,
                 drop=0.,
                 drop_path=0.,
                 act_layer='GELU',
                 norm_layer='LN',
                 post_norm=False,
                 offset_scale=0.5,
                 layer_scale=None,
                 with_cp=False,
                 dcn_output_bias=False,
                 mlp_fc2_bias=False,
                 dw_kernel_size=None, # for InternImage-H/G
                 post_norm_block_ids=None, # for InternImage-H/G
                 res_post_norm=False, # for InternImage-H/G
                 center_feature_scale=False): # for InternImage-H/G
        super().__init__()
        self.channels = channels
        self.depth = depth
        self.post_norm = post_norm
        self.center_feature_scale = center_feature_scale
        self.grad_checkpoint = False

        self.blocks = nn.ModuleList([
            InternImageLayer(
                core_op=core_op,
                channels=channels,
                groups=groups,
                mlp_ratio=mlp_ratio,
                drop=drop,
                drop_path=drop_path[i] if isinstance(
                    drop_path, list) else drop_path,
                act_layer=act_layer,
                norm_layer=norm_layer,
                post_norm=post_norm,
                layer_scale=layer_scale,
                offset_scale=offset_scale,
                with_cp=with_cp,
                dcn_output_bias=dcn_output_bias,
                mlp_fc2_bias=mlp_fc2_bias,
                dw_kernel_size=dw_kernel_size, # for InternImage-H/G
                res_post_norm=res_post_norm, # for InternImage-H/G
                center_feature_scale=center_feature_scale # for InternImage-H/G
            ) for i in range(depth)
        ])
        if not self.post_norm or center_feature_scale:
            self.norm = build_norm_layer(channels, 'LN')
        self.post_norm_block_ids = post_norm_block_ids
        if post_norm_block_ids is not None: # for InternImage-H/G
            self.post_norms = nn.ModuleList(
                [build_norm_layer(channels, 'LN', eps=1e-6) for _ in post_norm_block_ids]
            )
        self.downsample = downsample_layer(
            channels=channels, norm_layer=norm_layer) if downsample else None


    def forward(self, x, return_wo_downsample=False, shape=None, level_idx=0
    ):
        for i, blk in enumerate(self.blocks):
            if self.grad_checkpoint and not torch.jit.is_scripting():
                x = checkpoint_seq(blk, x)
            else:
                x = blk(x, shape=shape, level_idx=level_idx)
            if (self.post_norm_block_ids is not None) and (i in self.post_norm_block_ids):
                index = self.post_norm_block_ids.index(i)
                x = self.post_norms[index](x) # for InternImage-H/G
        if not self.post_norm or self.center_feature_scale:
            x = self.norm(x)
        if return_wo_downsample:
            x_ = x.clone()
        if self.downsample is not None:
            x, shape = self.downsample(x, shape=shape)

        if return_wo_downsample:
            return x, x_, shape
        return x, shape
    

class FlashInternImage(nn.Module):
    r""" FlashInternImage
        A PyTorch impl based on :
            `InternImage: Exploring Large-Scale Vision Foundation Models with Deformable Convolutions`  -
            https://arxiv.org/pdf/2103.14030
            `DCNv4` - https://arxiv.org/pdf/2401.06197
    Args:
        core_op (str): Core operator. Default: 'DCNv4'
        channels (int): Number of the first stage. Default: 64
        depths (list): Depth of each block. Default: [3, 4, 18, 5]
        groups (list): Groups of each block. Default: [3, 6, 12, 24]
        num_classes (int): Number of classes. Default: 1000
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim. Default: 4.
        drop_rate (float): Probability of an element to be zeroed. Default: 0.
        drop_path_rate (float): Stochastic depth rate. Default: 0.2
        drop_path_type (str): Drop path type. Default: 'linear'
        act_layer (str): Activation layer. Default: 'GELU'
        norm_layer (str): Normalization layer. Default: 'LN'
        layer_scale (float): Layer scale. Default: None
        offset_scale (float): Offset scale. Default: 0.5
        post_norm (bool): Whether to use post norm. Default: False
        cls_scale (float): Class scale. Default: 1.5
        with_cp (bool): Use checkpoint or not. Using checkpoint will save some
        mlp_fc2_bias (bool): Whether to use mlp fc2 bias. Default: False
        dcn_output_bias (bool): Whether to use dcn output bias. Default: False
        dw_kernel_size (int): Size of the dwconv. Default: None
        use_clip_projector (bool): Whether to use clip projector. Default: False
        level2_post_norm (bool): Whether to use level2 post norm. Default: False
        level2_post_norm_block_ids (list): Indexes of post norm blocks. Default: None
        res_post_norm (bool): Whether to use res post norm. Default: False
        center_feature_scale (bool): Whether to use center feature scale. Default: False
        out_indices (tuple): Output from which stages. Default: (0, 1, 2, 3)
    """

    def __init__(self,
                 core_op='DCNv4',
                 channels=64,
                 depths=[3, 4, 18, 5],
                 groups=[3, 6, 12, 24],
                 num_classes=1000,
                 mlp_ratio=4.,
                 drop_rate=0.,
                 drop_path_rate=0.2,
                 drop_path_type='linear',
                 act_layer='GELU',
                 norm_layer='LN',
                 layer_scale=None,
                 offset_scale=0.5,
                 post_norm=False,
                 cls_scale=1.5,
                 with_cp=False,
                 mlp_fc2_bias=False,
                 dcn_output_bias=False,
                 dw_kernel_size=None,
                 use_clip_projector=False, # for InternImage-H/G
                 level2_post_norm=False, # for InternImage-H/G
                 level2_post_norm_block_ids=None, # for InternImage-H/G
                 res_post_norm=False, # for InternImage-H/G
                 center_feature_scale=False, # for InternImage-H/G
                 out_indices=(0, 1, 2, 3),
                 **kwargs):
        super().__init__()
        if dcn_version == 'DCNv4' and core_op == 'DCNv4':
            core_op = 'DCNv4'
        else:
            warnings.warn('FlashInternImage requires DCNv4, but not found in current enviroment.\n\
                By default using DCNv3 pure pytorch implementation instead, which will affect the performance.\n\
                Suggesting install DCNv4 by `pip install DCNv4`')
            core_op = 'DCNv3'
        self.core_op = core_op
        self.num_classes = num_classes
        self.num_levels = len(depths)
        self.depths = depths
        self.channels = channels
        self.num_features = int(channels * 2**(self.num_levels - 1))
        self.post_norm = post_norm
        self.mlp_ratio = mlp_ratio
        self.use_clip_projector = use_clip_projector
        self.level2_post_norm_block_ids = level2_post_norm_block_ids
        self.out_indices = out_indices
        _logger.info(f'use core type: {core_op}')
        _logger.info(f'using activation layer: {act_layer}')
        _logger.info(f'using main norm layer: {norm_layer}')
        _logger.info(f'using dpr: {drop_path_type}, {drop_path_rate}')
        _logger.info(f'level2_post_norm: {level2_post_norm}')
        _logger.info(f'level2_post_norm_block_ids: {level2_post_norm_block_ids}')
        _logger.info(f'res_post_norm: {res_post_norm}')

        in_chans = 3
        self.patch_embed = StemLayer(in_chans=in_chans,
                                     out_chans=channels,
                                     act_layer=act_layer,
                                     norm_layer=norm_layer)
        self.pos_drop = nn.Dropout(p=drop_rate)

        dpr = [
            x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))
        ]
        if drop_path_type == 'uniform':
            for i in range(len(dpr)):
                dpr[i] = drop_path_rate
   
        self.levels = nn.ModuleList()
        for i in range(self.num_levels):
            post_norm_block_ids = level2_post_norm_block_ids if level2_post_norm and (
                i == 2) else None # for InternImage-H/G

            level = InternImageBlock(
                core_op=core_op,
                channels=int(channels * 2**i),
                depth=depths[i],
                groups=groups[i],
                mlp_ratio=self.mlp_ratio,
                drop=drop_rate,
                drop_path=dpr[sum(depths[:i]):sum(depths[:i + 1])],
                act_layer=act_layer,
                norm_layer=norm_layer,
                post_norm=post_norm,
                downsample=(i < self.num_levels - 1),
                downsample_layer = DownsampleLayer,
                layer_scale=layer_scale,
                offset_scale=offset_scale,
                with_cp=with_cp,
                mlp_fc2_bias=mlp_fc2_bias,
                dcn_output_bias=dcn_output_bias,
                dw_kernel_size=dw_kernel_size,  # for InternImage-H/G
                post_norm_block_ids=post_norm_block_ids, # for InternImage-H/G
                res_post_norm=res_post_norm, # for InternImage-H/G
                center_feature_scale=center_feature_scale # for InternImage-H/G
            )
            self.levels.append(level)
        
        if not use_clip_projector: # for InternImage-T/S/B/L/XL
            self.conv_head = nn.Sequential(
                nn.Conv2d(self.num_features,
                          int(self.num_features * cls_scale),
                          kernel_size=1,
                          bias=False),
                build_norm_layer(int(self.num_features * cls_scale), 'BN',
                                 'channels_first', 'channels_first'),
                build_act_layer(act_layer))
            self.head = nn.Linear(int(self.num_features * cls_scale), num_classes) \
                if num_classes > 0 else nn.Identity()
        else: # for InternImage-H/G
            pretrain_embed_dim, _stride, attnpool_num_heads, clip_embed_dim = 1024, 2, 16, 768
            self.dcnv3_head_x4 = nn.Sequential(
                nn.Conv2d(in_channels=self.num_features,
                          out_channels=pretrain_embed_dim * (_stride ** 2),
                          kernel_size=1), nn.PixelShuffle(_stride))
            self.dcnv3_head_x3 = nn.Conv2d(in_channels=self.num_features // 2,
                                           out_channels=pretrain_embed_dim,
                                           kernel_size=1)
            self.clip_projector = AttentionPoolingBlock(
                dim=pretrain_embed_dim,
                num_heads=attnpool_num_heads,
                qkv_bias=True,
                qk_scale=None,
                drop=0.,
                attn_drop=0.,
                norm_layer=norm_layer,
                out_dim=clip_embed_dim)
            self.fc_norm = build_norm_layer(clip_embed_dim, norm_layer, eps=1e-6)
            self.head = nn.Linear(
                clip_embed_dim, num_classes) if num_classes > 0 else nn.Identity()
            
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.num_layers = len(depths)
        self.apply(self._init_weights)
        self.apply(self._init_deform_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def _init_deform_weights(self, m):
        if dcn_version == 'DCNv4' and isinstance(m, getattr(DCNv4, self.core_op)):
            m._reset_parameters()
        elif isinstance(m, DCNv3_pytorch):
            m._reset_parameters()

    def init_weights(self):
        self.apply(self._init_weights)
        self.apply(self._init_deform_weights)

    @torch.jit.ignore
    def get_classifier(self):
        return self.head
    
    def reset_classifier(self, num_classes, global_pool=None):
        self.num_classes = num_classes
        self.head = nn.Linear(self.num_features, num_classes) \
            if num_classes > 0 else nn.Identity()
        
    @torch.jit.ignore
    def group_matcher(self, coarse: bool = False) -> Dict:
        return dict(
            stem=r'^patch_embed',  # stem and embed
            blocks=[(r'^levels\.(\d+)', None)]
        )
    
    @torch.jit.ignore
    def set_grad_checkpointing(self, enable=True):
        for l in self.levels:
            l.grad_checkpointing = enable

    @torch.jit.ignore
    def lr_decay_keywards(self, decay_ratio=0.87):
        lr_ratios = {}

        # blocks
        idx = 0
        for i in range(4):
            layer_num = 3 - i  # 3 2 1 0
            for j in range(self.depths[layer_num]):
                block_num = self.depths[layer_num] - j - 1
                tag = 'levels.{}.blocks.{}.'.format(layer_num, block_num)
                decay = 1.0 * (decay_ratio**idx)
                lr_ratios[tag] = decay
                idx += 1
        # patch_embed (before stage-1)
        lr_ratios["patch_embed"] = lr_ratios['levels.0.blocks.0.']
        # levels.0.downsample (between stage-1 and stage-2)
        lr_ratios["levels.0.downsample"] = lr_ratios['levels.1.blocks.0.']
        lr_ratios["levels.0.norm"] = lr_ratios['levels.1.blocks.0.']
        # levels.1.downsample (between stage-2 and stage-3)
        lr_ratios["levels.1.downsample"] = lr_ratios['levels.2.blocks.0.']
        lr_ratios["levels.1.norm"] = lr_ratios['levels.2.blocks.0.']
        # levels.2.downsample (between stage-3 and stage-4)
        lr_ratios["levels.2.downsample"] = lr_ratios['levels.3.blocks.0.']
        lr_ratios["levels.2.norm"] = lr_ratios['levels.3.blocks.0.']
        return lr_ratios

    def forward_features_no_clip_projector(self, x):
        x = self.patch_embed(x)
        N, H, W, C = x.shape
        x = x.view(N, H*W, C)

        shape=(H, W)
        seq_out = []
        for level_idx, level in enumerate(self.levels):
            old_shape = shape
            x, shape = level(x, shape=shape)   
        h, w = shape
        x = x.view(N, h, w, -1)
        x = self.conv_head(x.permute(0, 3, 1, 2))
        # x = self.avgpool(x)
        # x = torch.flatten(x, 1)
        return x

    def forward_features_seq_out(self, x): # for detection or segmentation
        x = self.patch_embed(x)
        N, H, W, C = x.shape
        x = x.view(N, H*W, C)
        shape=(H, W)
        seq_out = []
        for level_idx, level in enumerate(self.levels):
            old_shape = shape
            x, x_ , shape = level(x, return_wo_downsample=True, shape=shape, level_idx=level_idx) 
            h, w= old_shape
            seq_out.append(x_.reshape(N, h, w, -1).permute(0, 3, 1, 2))
        return seq_out
    
    def forward_clip_projector(self, x): # for InternImage-H/G
        xs = self.forward_features_seq_out(x)
        x1, x2, x3, x4 = xs
        
        x1 = x1.permute(0, 3, 1, 2) # NHWC -> NCHW
        x2 = x2.permute(0, 3, 1, 2) # NHWC -> NCHW
        x3 = x3.permute(0, 3, 1, 2) # NHWC -> NCHW
        x4 = x4.permute(0, 3, 1, 2) # NHWC -> NCHW

        x4 = self.dcnv3_head_x4(x4)
        x = x4
        x3 = self.dcnv3_head_x3(x3)
        x = x + x3

        # x = x.flatten(-2).transpose(1, 2).contiguous()
        # x = self.clip_projector(x)
        # x = self.fc_norm(x)
        
        return x
    
    def forward_features(self, x):
        if self.use_clip_projector: # for InternImage-H/G
            x = self.forward_clip_projector(x)
        else: # for InternImage-T/S/B/L/XL
            x = self.forward_features_no_clip_projector(x)
        return x
    
    def forward(self, x):
        x = self.forward_features(x)
        if self.use_clip_projector:
            x = x.flatten(-2).transpose(1, 2).contiguous()
            x = self.clip_projector(x)
            x = self.fc_norm(x)
        else:
            x = self.avgpool(x)
            x = torch.flatten(x, 1)
        x = self.head(x)
        return x


def checkpoint_filter_fn(state_dict, model):
    """ process different state_dict format from different pretaied models """
    if 'model' in state_dict:
        _state_dict = state_dict['model']
    elif 'state_dict' in state_dict:
        _state_dict = state_dict['state_dict']
    else:
        raise ValueError('Unrecognized state_dict format')
    
    state_dict = OrderedDict()
    for k, v in _state_dict.items():
        if k.startswith('backbone.'):
            k = k[9:]
        state_dict[k] = v

    if list(state_dict.keys())[0].startswith('module.'):
        state_dict = {k[7:]: v for k, v in state_dict.items()}

    return state_dict


def _cfg(url: str = '', **kwargs) -> Dict[str, Any]:
    return {
        'url': url,
        'num_classes': 1000,
        'input_size': (3, 224, 224),
        'pool_size': None,
        'crop_pct': 0.9,
        'interpolation': 'bicubic',
        'fixed_input_size': True,
        'mean': IMAGENET_DEFAULT_MEAN,
        'std': IMAGENET_DEFAULT_STD,
        'first_conv': 'patch_embed.conv1',
        'classifier': 'head',
        **kwargs,
    }


default_cfgs = generate_default_cfgs({
    'flash_intern_image_tiny.224_in1k': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/flash_intern_image_t_1k_224.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='flash_intern_image_t_1k_224.pth'
    ),
    'flash_intern_image_small.224_in1k': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/flash_intern_image_s_1k_224.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='flash_intern_image_s_1k_224.pth'
    ),
    'flash_intern_image_base.224_in1k': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/flash_intern_image_b_1k_224.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='flash_intern_image_b_1k_224.pth'
    ),
    'flash_intern_image_large.384_in22k_ft_1k': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/flash_intern_image_l_22kto1k_384.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='flash_intern_image_l_22kto1k_384.pth',
        input_size=(3, 384, 384),
    ),
    'flash_intern_image_large.384_in22k': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/flash_intern_image_l_22k_384.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='flash_intern_image_l_22k_384.pth',
        input_size=(3, 384, 384),
        num_classes=21841,
    ),
    'cascade_flash_intern_image_large.fpn_1x_coco': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/cascade_flash_internimage_l_fpn_1x_coco.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='cascade_flash_internimage_l_fpn_1x_coco.pth',
    ),
    'cascade_flash_intern_image_large.fpn_3x_coco': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/cascade_flash_internimage_l_fpn_3x_coco.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='cascade_flash_internimage_l_fpn_3x_coco.pth',
    ),
    'dino_4scale_flash_intern_image_tiny.1x_coco': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/dino_4scale_flash_internimage_t_1x_coco.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='dino_4scale_flash_internimage_t_1x_coco.pth',
    ),
    'dino_4scale_flash_intern_image_small.1x_coco': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/dino_4scale_flash_internimage_s_1x_coco.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='dino_4scale_flash_internimage_s_1x_coco.pth',
    ),
    'dino_4scale_flash_intern_image_base.1x_coco': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/dino_4scale_flash_internimage_b_1x_coco.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='dino_4scale_flash_internimage_b_1x_coco.pth',
    ),
    'dino_4scale_flash_intern_image_large.1x_coco': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/dino_4scale_flash_internimage_l_1x_coco.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='dino_4scale_flash_internimage_l_1x_coco.pth',
        input_size=(3, 384, 384),
        num_classes=21841,
    ),
    'mask_rcnn_flash_intern_image_tiny.fpn_1x_coco': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/mask_rcnn_flash_internimage_t_fpn_1x_coco.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='mask_rcnn_flash_internimage_t_fpn_1x_coco.pth',
    ),
    'mask_rcnn_flash_intern_image_tiny.fpn_3x_coco': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/mask_rcnn_flash_internimage_s_fpn_3x_coco.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='mask_rcnn_flash_internimage_s_fpn_3x_coco.pth',
    ),
    'mask_rcnn_flash_intern_image_small.fpn_1x_coco': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/mask_rcnn_flash_internimage_s_fpn_1x_coco.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='mask_rcnn_flash_internimage_s_fpn_1x_coco.pth',
    ),
    'mask_rcnn_flash_intern_image_small.fpn_3x_coco': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/mask_rcnn_flash_internimage_s_fpn_3x_coco.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='mask_rcnn_flash_internimage_s_fpn_3x_coco.pth',
    ),
    'mask_rcnn_flash_intern_image_base.fpn_1x_coco': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/mask_rcnn_flash_internimage_b_fpn_1x_coco.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='mask_rcnn_flash_internimage_b_fpn_1x_coco.pth',
    ),
    'mask_rcnn_flash_intern_image_base.fpn_3x_coco': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/mask_rcnn_flash_internimage_b_fpn_3x_coco.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='mask_rcnn_flash_internimage_b_fpn_3x_coco.pth',
    ),
    'mask2former_flash_intern_image_tiny.512_160k_ade20k_ss': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/mask2former_flash_internimage_t_512_160k_ade20k_ss.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='mask2former_flash_internimage_t_512_160k_ade20k_ss.pth',
        input_size=(3, 512, 512),
    ),
    'mask2former_flash_intern_image_small.640_160k_ade20k_ss': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/mask2former_flash_internimage_s_640_160k_ade20k_ss.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='mask2former_flash_internimage_s_640_160k_ade20k_ss.pth',
        input_size=(3, 640, 640),
    ),
    'mask2former_flash_intern_image_base.640_160k_ade20k_ss': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/mask2former_flash_internimage_b_640_160k_ade20k_ss.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='mask2former_flash_internimage_b_640_160k_ade20k_ss.pth',
        input_size=(3, 640, 640),
    ),
    'mask2former_flash_intern_image_large.640_160k_ade20k_ss': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/mask2former_flash_internimage_l_640_160k_ade20k_ss.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='mask2former_flash_internimage_l_640_160k_ade20k_ss.pth',
        input_size=(3, 640, 640),
    ),
    'upernet_flash_intern_image_tiny.512_160k_ade20k': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/upernet_flash_internimage_t_512_160k_ade20k.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='upernet_flash_internimage_t_512_160k_ade20k.pth',
        input_size=(3, 512, 512),
    ),
    'upernet_flash_intern_image_small.512_160k_ade20k': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/upernet_flash_internimage_s_512_160k_ade20k.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='upernet_flash_internimage_s_512_160k_ade20k.pth',
        input_size=(3, 512, 512),
    ),
    'upernet_flash_intern_image_base.512_160k_ade20k': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/upernet_flash_internimage_b_512_160k_ade20k.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='upernet_flash_internimage_b_512_160k_ade20k.pth',
        input_size=(3, 512, 512),
    ),
    'upernet_flash_intern_image_large.640_160k_ade20k': _cfg(
        url='https://huggingface.co/OpenGVLab/DCNv4/blob/main/upernet_flash_internimage_l_640_160k_ade20k.pth',
        hf_hub_id='OpenGVLab/DCNv4',
        hf_hub_filename='upernet_flash_internimage_l_640_160k_ade20k.pth',
        input_size=(3, 640, 640),
    ),
})


def _create_flash_intern_image(variant: str, pretrained: bool = False, **kwargs):
    default_out_indices = tuple(i for i, _ in enumerate(kwargs.get('depths', (1, 1, 1, 1))))
    out_indices = kwargs.pop('out_indices', default_out_indices)
    return build_model_with_cfg(
        FlashInternImage,
        variant,
        pretrained=pretrained,
        pretrained_filter_fn=checkpoint_filter_fn,
        pretrained_strict=False,
        feature_cfg=dict(flatten_sequential=True, out_indices=out_indices),
        **kwargs
    )


def _check_pretrained_available(pretrained: bool):
    if dcn_version == 'DCNv4':
        return pretrained

    warnings.warn('DCNv4 is not installed, cannot load pretrained weights')
    return False


@register_model
def flash_intern_image_tiny(pretrained=False, **kwarg):
    """ 
    FlashInternImage-T, trained on ImageNet-1k, for classification.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=64,
        depths=[4, 4, 18, 4],
        groups=[4, 8, 16, 32],
        offset_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.1,
    )
    return _create_flash_intern_image('flash_intern_image_tiny', pretrained=pretrained, **dict(model_arg, **kwarg))


@register_model
def flash_intern_image_small(pretrained=False, **kwarg):
    """
    FlashInternImage-S, trained on ImageNet-1k, for classification.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=80,
        depths=[4, 4, 21, 4],
        groups=[5, 10, 20, 40],
        layer_scale=1e-5,
        offset_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.4,
        post_norm=True,
        dw_kernel_size=3,
    )
    return _create_flash_intern_image('flash_intern_image_small', pretrained=pretrained, **dict(model_arg, **kwarg))


@register_model
def flash_intern_image_base(pretrained=False, **kwarg):
    """
    FlashInternImage-B, trained on ImageNet-1k, for classification.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=112,
        depths=[4, 4, 21, 4],
        groups=[7, 14, 28, 56],
        layer_scale=1e-5,
        offset_scale=0.5,
        mlp_ratio=4.,
        drop_path_rate=0.5,
        post_norm=True,
        dw_kernel_size=3,
    )
    return _create_flash_intern_image('flash_intern_image_base', pretrained=pretrained, **dict(model_arg, **kwarg))


@register_model
def flash_intern_image_large(pretrained=False, **kwarg):
    """
    FlashInternImage-L, trained on ImageNet-1k, for classification.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=160,
        depths=[5, 5, 22, 5],
        groups=[10, 20, 40, 80],
        layer_scale=1e-5,
        offset_scale=2.,
        mlp_ratio=4.,
        drop_path_rate=0.1,
        post_norm=True,
        dw_kernel_size=3,
        dcn_output_bias=True,
        mlp_fc2_bias=True,
    )
    return _create_flash_intern_image('flash_intern_image_large', pretrained=pretrained, **dict(model_arg, **kwarg))


@register_model
def cascade_flash_intern_image_large(pretrained=False, **kwargs):
    """
    CascadeFlashInternImage-L, trained on COCO, used as backbone for detection.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=160,
        depths=[5, 5, 22, 5],
        groups=[10, 20, 40, 80],
        layer_scale=1.,
        offset_scale=2.,
        mlp_ratio=4.,
        drop_path_rate=0.4,
        post_norm=True,
        dw_kernel_size=3,
        dcn_output_bias=True,
        mlp_fc2_bias=True,
        out_indices=(0, 1, 2, 3),
    )
    return _create_flash_intern_image('cascade_flash_intern_image_large', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def dino_4scale_flash_intern_image_tiny(pretrained=False, **kwargs):
    """
    FlashInternImage-T, trained on ImageNet-1K, used as backbone for detection.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=64,
        depths=[4, 4, 18, 4],
        groups=[4, 8, 16, 32],
        offset_scale=1.,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.2,
        pose_norm=False,
        with_cp=True,
        output_indices=(1, 2, 3),
    )
    return _create_flash_intern_image('dino_4scale_flash_intern_image_tiny', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def dino_4scale_flash_intern_image_small(pretrained=False, **kwargs):
    """
    FlashInternImage-S, trained on ImageNet-1K, used as backbone for detection.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=80,
        depths=[4, 4, 21, 4],
        groups=[5, 10, 20, 40],
        offset_scale=1.,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.3,
        pose_norm=True,
        with_cp=True,
        dw_kernel_size=3,
        output_indices=(1, 2, 3),
    )
    return _create_flash_intern_image('dino_4scale_flash_intern_image_small', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def dino_4scale_flash_intern_image_base(pretrained=False, **kwargs):
    """
    FlashInternImage-B, trained on ImageNet-1K, used as backbone for detection.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=112,
        depths=[4, 4, 21, 4],
        groups=[7, 14, 28, 56],
        offset_scale=0.5,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.3,
        pose_norm=True,
        with_cp=True,
        dw_kernel_size=3,
        output_indices=(1, 2, 3),
    )
    return _create_flash_intern_image('dino_4scale_flash_intern_image_base', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def dino_4scale_flash_intern_image_large(pretrained=False, **kwargs):
    """
    FlashInternImage-L, trained on ImageNet-22K, used as backbone for detection.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=160,
        depths=[5, 5, 22, 5],
        groups=[10, 20, 40, 80],
        offset_scale=2.,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.4,
        pose_norm=True,
        with_cp=True,
        dw_kernel_size=3,
        dcn_output_bias=True,
        mlp_fc2_bias=True,
        output_indices=(1, 2, 3),
    )
    return _create_flash_intern_image('dino_4scale_flash_intern_image_large', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def mask_rcnn_flash_intern_image_tiny(pretrained=False, **kwargs):
    """
    FlashInternImage-T, trained on COCO, used as backbone for detection.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=64,
        depths=[4, 4, 18, 4],
        groups=[4, 8, 16, 32],
        offset_scale=1.,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.2,
        pose_norm=False,
        with_cp=True,
        output_indices=(0, 1, 2, 3),
    )
    return _create_flash_intern_image('mask_rcnn_flash_intern_image_tiny', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def mask_rcnn_flash_intern_image_small(pretrained=False, **kwargs):
    """
    FlashInternImage-S, trained on COCO, used as backbone for detection.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=80,
        depths=[4, 4, 21, 4],
        groups=[5, 10, 20, 40],
        offset_scale=1.,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.3,
        pose_norm=True,
        with_cp=True,
        dw_kernel_size=3,
        output_indices=(0, 1, 2, 3),
    )
    return _create_flash_intern_image('mask_rcnn_flash_intern_image_small', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def mask_rcnn_flash_intern_image_base(pretrained=False, **kwargs):
    """
    FlashInternImage-B, trained on COCO, used as backbone for detection.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=112,
        depths=[4, 4, 21, 4],
        groups=[7, 14, 28, 56],
        offset_scale=0.5,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.3,
        pose_norm=True,
        with_cp=True,
        dw_kernel_size=3,
        output_indices=(0, 1, 2, 3),
    )
    return _create_flash_intern_image('mask_rcnn_flash_intern_image_base', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def mask2former_flash_intern_image_tiny(pretrained=False, **kwargs):
    """
    FlashInternImage-T, trained on ADE20K, used as backbone for segmentation.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=64,
        depths=[4, 4, 18, 4],
        groups=[4, 8, 16, 32],
        offset_scale=1.,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.2,
        pose_norm=False,
        with_cp=False,
        output_indices=(0, 1, 2, 3),
    )
    return _create_flash_intern_image('mask2former_flash_intern_image_tiny', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def mask2former_flash_intern_image_small(pretrained=False, **kwargs):
    """
    FlashInternImage-S, trained on ADE20K, used as backbone for segmentation.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=80,
        depths=[4, 4, 21, 4],
        groups=[5, 10, 20, 40],
        offset_scale=1.,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.3,
        pose_norm=True,
        with_cp=False,
        dw_kernel_size=3,
        output_indices=(0, 1, 2, 3),
    )
    return _create_flash_intern_image('mask2former_flash_intern_image_small', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def mask2former_flash_intern_image_base(pretrained=False, **kwargs):
    """
    FlashInternImage-B, trained on ADE20K, used as backbone for segmentation.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=112,
        depths=[4, 4, 21, 4],
        groups=[7, 14, 28, 56],
        offset_scale=0.5,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.4,
        pose_norm=True,
        with_cp=False,
        dw_kernel_size=3,
        output_indices=(0, 1, 2, 3),
    )
    return _create_flash_intern_image('mask2former_flash_intern_image_base', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def mask2former_flash_intern_image_large(pretrained=False, **kwargs):
    """
    FlashInternImage-L, trained on ADE20K, used as backbone for segmentation.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=160,
        depths=[5, 5, 22, 5],
        groups=[10, 20, 40, 80],
        offset_scale=2.,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.5,
        pose_norm=True,
        with_cp=True,
        dw_kernel_size=3,
        dcn_output_bias=True,
        mlp_fc2_bias=True,
        output_indices=(0, 1, 2, 3),
    )
    return _create_flash_intern_image('mask2former_flash_intern_image_large', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def upernet_flash_intern_image_tiny(pretrained=False, **kwargs):
    """
    FlashInternImage-T, trained on ADE20K, used as backbone for segmentation.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=64,
        depths=[4, 4, 18, 4],
        groups=[4, 8, 16, 32],
        offset_scale=1.,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.2,
        pose_norm=False,
        with_cp=True,
        output_indices=(0, 1, 2, 3),
    )
    return _create_flash_intern_image('upernet_flash_intern_image_tiny', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def upernet_flash_intern_image_small(pretrained=False, **kwargs):
    """
    FlashInternImage-S, trained on ADE20K, used as backbone for segmentation.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=80,
        depths=[4, 4, 21, 4],
        groups=[5, 10, 20, 40],
        offset_scale=1.,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.3,
        pose_norm=True,
        with_cp=True,
        dw_kernel_size=3,
        output_indices=(0, 1, 2, 3),
    )
    return _create_flash_intern_image('upernet_flash_intern_image_small', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def upernet_flash_intern_image_base(pretrained=False, **kwargs):
    """
    FlashInternImage-B, trained on ADE20K, used as backbone for segmentation.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=112,
        depths=[4, 4, 21, 4],
        groups=[7, 14, 28, 56],
        offset_scale=0.5,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.3,
        pose_norm=True,
        with_cp=False,
        dw_kernel_size=3,
        output_indices=(0, 1, 2, 3),
    )
    return _create_flash_intern_image('upernet_flash_intern_image_base', pretrained=pretrained, **dict(model_arg, **kwargs))


@register_model
def upernet_flash_intern_image_large(pretrained=False, **kwargs):
    """
    FlashInternImage-L, trained on ADE20K, used as backbone for segmentation.
    """
    pretrained = _check_pretrained_available(pretrained)
    model_arg = dict(
        core_op='DCNv4',
        channels=160,
        depths=[5, 5, 22, 5],
        groups=[10, 20, 40, 80],
        offset_scale=2.,
        layer_scale=1.,
        mlp_ratio=4.,
        drop_path_rate=0.4,
        pose_norm=True,
        with_cp=False,
        dw_kernel_size=3,
        dcn_output_bias=True,
        mlp_fc2_bias=True,
        output_indices=(0, 1, 2, 3),
    )
    return _create_flash_intern_image('upernet_flash_intern_image_large', pretrained=pretrained, **dict(model_arg, **kwargs))
