# Copyright (c) Zixuan Liu et al, OCTCubeM group
# All rights reserved.


# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# timm: https://github.com/rwightman/pytorch-image-models/tree/master/timm
# DeiT: https://github.com/facebookresearch/deit
# MAE: https://github.com/facebookresearch/mae

# Revised by Zixuan Zucks Liu @University of Washington
# --------------------------------------------------------



from functools import partial
from typing import Callable, Optional, Sequence
import re
import torch
import torch.nn as nn
from .misc import master_print as print
from einops import rearrange
from collections import OrderedDict
from .video_vit import Attention, Block, PatchEmbed
from flash_attn.models.vit import create_block



class VisionTransformer(nn.Module):
    """Vision Transformer with support for global average pooling"""

    def __init__(
        self,
        num_frames,
        t_patch_size,
        image_size=256,
        patch_size=16,
        in_chans=1,
        out_dim=512,
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
        # dropout=0.5,
        sep_pos_embed=False,
        cls_embed=True,
        global_pool=False,
        use_flash_attn=False,
        **kwargs,
    ):
        super().__init__()
        print(locals())
        print(t_patch_size, num_frames)
        self.image_size = image_size
        self.global_pool = global_pool
        print('global_pool', global_pool)

        self.sep_pos_embed = sep_pos_embed
        # --------------------------------------------------------------------------
        # MAE encoder specifics
        self.patch_embed = PatchEmbed(
            image_size, patch_size, in_chans, embed_dim, num_frames, t_patch_size
        )
        num_patches = self.patch_embed.num_patches
        input_size = self.patch_embed.input_size
        print(input_size)

        self.input_size = input_size
        self.cls_embed = cls_embed

        if self.cls_embed:
            self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))

        if sep_pos_embed:
            self.pos_embed_spatial = nn.Parameter(
                torch.zeros(1, input_size[1] * input_size[2], embed_dim)
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
            )  # fixed or not?

        dpr = [
            x.item() for x in torch.linspace(0, drop_path_rate, depth)
        ]  # stochastic depth decay rule

        self.use_flash_attn = use_flash_attn
        if True:
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
        self.final_act = nn.GELU()
        # Fully connected layer for the aggregated CLS tokens
        self.fc_aggregate_cls = nn.Linear(embed_dim, embed_dim)

        # Normalization layer after aggregation
        self.aggregate_cls_norm = norm_layer(embed_dim)

        # self.dropout = nn.Dropout(dropout)
        self.head = nn.Linear(embed_dim, out_dim)


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
        # embed patches

        x = self.patch_embed(x)
        N, T, L, C = x.shape  # T: temporal; L: spatial

        x = x.view([N, T * L, C])

        # append cls token
        if self.cls_embed:
            cls_token = self.cls_token
            cls_tokens = cls_token.expand(x.shape[0], -1, -1)
            x = torch.cat((cls_tokens, x), dim=1)

        if self.sep_pos_embed:
            pos_embed = self.pos_embed_spatial.repeat(
                1, self.input_size[0], 1
            ) + torch.repeat_interleave(
                self.pos_embed_temporal,
                self.input_size[1] * self.input_size[2],
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

        if hidden_states:
            return hidden_states_list

        if self.global_pool:
            x = x[:, 1:, :].mean(dim=1)  # global pool without cls token
            x = self.norm(x)
        else:
            # print('check if this is correct: go into cls token')
            x = x[:, 0]

        # classifier
        x = self.fc_aggregate_cls(x)
        x = self.aggregate_cls_norm(x)
        x = self.final_act(x)
        # x = self.dropout(x)
        x = self.head(x)

        return x

    def load_state_dict_to_backbone(self, state_dict, strict=False, filter_keys=[]):
        if "patch_embed.proj.weight" in state_dict:
            patch_embed_weight = state_dict["patch_embed.proj.weight"]
            if patch_embed_weight.dim() == 4:
                # convert from Conv2d to Linear
                state_dict["patch_embed.proj.weight"] = rearrange(
                    patch_embed_weight, "o c h w -> o (c h w)"
                )
        else:
            print("Skip loading patch_embed.proj.weight")
        # print('state_dict', state_dict['patch_embed.proj.weight'].shape)
        def key_mapping_attn(key):
            key = re.sub(r"blocks.(\d+).attn.proj.", r"blocks.\1.mixer.out_proj.", key)
            return key

        state_dict = OrderedDict((key_mapping_attn(k), v) for k, v in state_dict.items())
        n_layer = len(self.blocks)
        # Convert from Wqkv to Wq and Wkv for cross attention (last layer)
        for i in range(n_layer):
            Wq, Wk, Wv = state_dict.pop(f"blocks.{i}.attn.q.weight"), state_dict.pop(
                f"blocks.{i}.attn.k.weight"
            ), state_dict.pop(f"blocks.{i}.attn.v.weight")
            bq, bk, bv = state_dict.pop(f"blocks.{i}.attn.q.bias"), state_dict.pop(
                f"blocks.{i}.attn.k.bias"
            ), state_dict.pop(f"blocks.{i}.attn.v.bias")
            Wqkv = torch.cat([Wq, Wk, Wv], dim=0)
            bqkv = torch.cat([bq, bk, bv], dim=0)
            state_dict[f"blocks.{i}.mixer.Wqkv.weight"] = Wqkv
            state_dict[f"blocks.{i}.mixer.Wqkv.bias"] = bqkv


        # filter out pos_embed and patch_embed
        state_dict = {k: v for k, v in state_dict.items() if not any([f in k for f in filter_keys])}
        return super().load_state_dict(state_dict, strict=strict)

    #FIXME: add grad_checkpointing
    @torch.jit.ignore
    def set_grad_checkpointing(self, enable=True):
        self.blocks.grad_checkpointing = enable

    def lock(self, unlocked_groups=0, freeze_bn_stats=False):
        for param in self.parameters():
            param.requires_grad = False

        if unlocked_groups != 0:
            if self.sep_pos_embed:
                first_group = [
                    self.patch_embed,
                    self.pos_embed_spatial,
                    self.pos_embed_temporal,
                    self.pos_embed_class,
                ]
            else:
                first_group = [self.patch_embed, self.pos_embed]
            if self.cls_embed:
                first_group.append(self.cls_token)

            groups = [
                first_group,
                *self.blocks[:-1],
                [
                    self.blocks[-1],
                    self.norm,
                ],
                [
                    self.fc_aggregate_cls,
                    self.aggregate_cls_norm,
                    self.head,
                ]
            ]
            print(f"Unlocking {unlocked_groups} groups, len(groups)={len(groups)}")

            def _unlock(x):
                if isinstance(x, Sequence):
                    for g in x:
                        _unlock(g)
                else:
                    if isinstance(x, torch.nn.Parameter):
                        x.requires_grad = True
                    else:
                        for p in x.parameters():
                            p.requires_grad = True

            _unlock(groups[-unlocked_groups:])

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
