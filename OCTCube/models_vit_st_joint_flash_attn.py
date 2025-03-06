# Copyright (c) Zixuan Liu et al, OCTCubeM group
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Revised by Zixuan Zucks Liu @University of Washington

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# timm: https://github.com/rwightman/pytorch-image-models/tree/master/timm
# DeiT: https://github.com/facebookresearch/deit
# MAE: https://github.com/facebookresearch/mae
# --------------------------------------------------------

from functools import partial
import sys
import re
import torch
import torch.nn as nn

try:
    # Case 1: Running from OCTCubeM/
    from util.misc import master_print as print
    from util.video_vit import Attention, Block, PatchEmbed
except ModuleNotFoundError:
    try:
        # Case 2: Running from OCTCube/
        from .util.misc import master_print as print
        from .util.video_vit import Attention, Block, PatchEmbed
    except ImportError:
        # Case 3: Running standalone, fix path
        sys.path.append('../')  # Add OCTCubeM/ to path
        from util.misc import master_print as print
        from util.video_vit import Attention, Block, PatchEmbed


from einops import rearrange
from collections import OrderedDict

from flash_attn.models.vit import create_block


class VisionTransformer(nn.Module):
    """Vision Transformer with support for global average pooling"""

    def __init__(
        self,
        num_frames,
        t_patch_size,
        img_size=256,
        patch_size=16,
        in_chans=1,
        num_classes=400,
        embed_dim=768,
        depth=12,
        num_heads=12,
        mlp_ratio=4.0,
        no_qkv_bias=False,
        qk_scale=None,
        drop_rate=0.0,
        attn_drop_rate=0.0,
        drop_path_rate=0.0,
        norm_layer=nn.LayerNorm,
        dropout=0.5,
        sep_pos_embed=False,
        cls_embed=False,
        global_pool=False,
        use_high_res_patch_embed=False,
        use_flash_attn=True,
        high_res_img_size=512,
        **kwargs,
    ):
        super().__init__()
        print(locals())
        self.global_pool = global_pool
        self.sep_pos_embed = sep_pos_embed
        # --------------------------------------------------------------------------
        # MAE encoder specifics
        self.patch_embed = PatchEmbed(
            img_size, patch_size, in_chans, embed_dim, num_frames, t_patch_size
        )
        if use_high_res_patch_embed:
            self.high_res_patch_embed = PatchEmbed(
                high_res_img_size, patch_size, in_chans, embed_dim, num_frames, t_patch_size
            )
            self.high_res_img_size = high_res_img_size
            self.high_res_input_size = self.high_res_patch_embed.input_size
        num_patches = self.patch_embed.num_patches
        input_size = self.patch_embed.input_size
        self.input_size = input_size
        self.cls_embed = cls_embed

        if self.cls_embed:
            self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))

        if sep_pos_embed:
            self.pos_embed_spatial = nn.Parameter(
                torch.zeros(1, self.high_res_input_size[1] * self.high_res_input_size[2], embed_dim)
            )
            self.pos_embed_temporal = nn.Parameter(
                torch.zeros(1, input_size[0], embed_dim)
            )
            if self.cls_embed:
                self.pos_embed_class = nn.Parameter(torch.zeros(1, 1, embed_dim))
        else:
            if self.cls_embed:
                _num_patches = num_patches + 1
            else:
                _num_patches = num_patches

            self.pos_embed = nn.Parameter(
                torch.zeros(1, _num_patches, embed_dim), requires_grad=True
            )

        dpr = [
            x.item() for x in torch.linspace(0, drop_path_rate, depth)
        ]  # stochastic depth decay rule

        self.use_flash_attn = use_flash_attn
        if use_flash_attn:
            self.blocks = nn.ModuleList(
            [
                create_block(
                    embed_dim,
                    num_heads,
                    mlp_ratio,
                    not no_qkv_bias,
                    drop_rate,
                    attn_drop_rate,
                    drop_path1=dpr[i - 1] if i > 0 else 0.0,
                    drop_path2=dpr[i],
                    norm_layer=norm_layer,
                    act_layer=nn.GELU,
                    use_flash_attn=use_flash_attn,
                    fused_bias_fc=False,
                    fused_mlp=False,
                    fused_dropout_add_ln=False,
                    layer_idx=i,
                    n_layer=depth,
                    last_layer_subset=False,
                )
                for i in range(depth)
            ])
        else:
            self.blocks = nn.ModuleList(
                [
                    Block(
                        embed_dim,
                        num_heads,
                        mlp_ratio,
                        qkv_bias=not no_qkv_bias,
                        qk_scale=None,
                        norm_layer=norm_layer,
                        drop_path=dpr[i],
                        attn_func=partial(
                            Attention,
                            input_size=self.patch_embed.input_size,
                        ),
                    )
                    for i in range(depth)
                ]
            )
        self.norm = norm_layer(embed_dim)
        # --------------------------------------------------------------------------

        self.dropout = nn.Dropout(dropout)
        self.head = nn.Linear(embed_dim, num_classes)

        torch.nn.init.normal_(self.head.weight, std=0.02)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {
            "cls_token",
            "pos_embed",
            "pos_embed_spatial",
            "pos_embed_temporal",
            "pos_embed_class",
        }

    def forward(self, x, hidden_states=False):
        H, W = x.shape[-2:]
        if H == self.high_res_input_size[1] * self.high_res_patch_embed.patch_size[0]:
            high_res = True
            x = self.high_res_patch_embed(x)
        else:
            high_res = False
            x = self.patch_embed(x)

        N, T, L, C = x.shape  # T: temporal; L: spatial

        x = x.view([N, T * L, C])

        # append cls token
        if self.cls_embed:
            cls_token = self.cls_token
            cls_tokens = cls_token.expand(x.shape[0], -1, -1)
            x = torch.cat((cls_tokens, x), dim=1)

        if self.sep_pos_embed:
            if not high_res:
                # pool pos_embed to match the input size
                pos_embed = F.interpolate(
                    self.pos_embed_spatial.view(1, self.high_res_input_size[1], self.high_res_input_size[2], -1).permute(0, 3, 1, 2), [self.input_size[1], self.input_size[2]],mode='bicubic', align_corners=False
                ).permute(0, 2, 3, 1).view(1, self.input_size[1] * self.input_size[2], -1)

                pos_h, pos_w = self.input_size[1], self.input_size[2]

            else:
                pos_embed = self.pos_embed_spatial
                pos_h, pos_w = self.high_res_input_size[1], self.high_res_input_size[2]

            pos_embed = pos_embed.repeat(
                    1, T, 1
                ) + torch.repeat_interleave(
                    self.pos_embed_temporal,
                    pos_h * pos_w,
                    dim=1,
                )

            if self.cls_embed:
                pos_embed = torch.cat(
                    [
                        self.pos_embed_class.expand(pos_embed.shape[0], -1, -1),
                        pos_embed,
                    ],
                    1,
                )
        else:
            pos_embed = self.pos_embed[:, :, :]
        x = x + pos_embed

        # reshape to [N, T, L, C] or [N, T*L, C]
        if hasattr(self.blocks[0], "attn"):
            requires_t_shape = (
                len(self.blocks) > 0  # support empty decoder
                and hasattr(self.blocks[0].attn, "requires_t_shape")
                and self.blocks[0].attn.requires_t_shape
            )
        else:
            requires_t_shape = False
        if requires_t_shape:
            x = x.view([N, T, L, C])

        # apply Transformer blocks
        hidden_states_list = []
        if self.use_flash_attn:
            residual = None
            for blk in self.blocks:
                x, residual = blk(x, residual)
                hidden_states_list.append(x)
        else:
            for blk in self.blocks:
                x = blk(x)
                hidden_states_list.append(x)


        if requires_t_shape:
            x = x.view([N, T * L, C])

        if self.global_pool:
            x = x[:, 1:, :].mean(dim=1)  # global pool without cls token
            outcome = self.norm(x)
        else:
            x = self.norm(x)
            outcome = x[:, 0]

        if hidden_states:
            return hidden_states_list

        # classifier
        x = self.dropout(x)
        x = self.head(x)
        return x


def vit_base_patch16(**kwargs):
    model = VisionTransformer(
        patch_size=16,
        embed_dim=768,
        depth=12,
        num_heads=12,
        mlp_ratio=4,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),
        **kwargs,
    )
    return model


def vit_large_patch16(**kwargs):
    model = VisionTransformer(
        patch_size=16,
        embed_dim=1024,
        depth=24,
        num_heads=16,
        mlp_ratio=4,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),
        # assume kwargs has global_pool
        # assume kwargs has sep_pos_embed
        # assume kwargs has cls_embed
        **kwargs,
    )
    return model

def flash_attn_vit_large_patch16(**kwargs):
    model = VisionTransformer(
        patch_size=16,
        embed_dim=1024,
        depth=24,
        num_heads=16,
        mlp_ratio=4,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),
        use_flash_attn=True,
        **kwargs,
    )
    return model


def vit_huge_patch14(**kwargs):
    model = VisionTransformer(
        patch_size=16,
        embed_dim=1280,
        depth=32,
        num_heads=16,
        mlp_ratio=4,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),
        **kwargs,
    )
    return model
