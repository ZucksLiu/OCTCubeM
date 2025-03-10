# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
# Partly revised by YZ @UCL&Moorfields
# --------------------------------------------------------

import numpy as np
import torch


# --------------------------------------------------------
# 2D sine-cosine position embedding
# References:
# Transformer: https://github.com/tensorflow/models/blob/master/official/nlp/transformer/model_utils.py
# MoCo v3: https://github.com/facebookresearch/moco-v3
# --------------------------------------------------------
def get_2d_sincos_pos_embed(embed_dim, grid_size, cls_token=False):
    """
    grid_size: int of the grid height and width
    return:
    pos_embed: [grid_size*grid_size, embed_dim] or [1+grid_size*grid_size, embed_dim] (w/ or w/o cls_token)
    """
    grid_h = np.arange(grid_size, dtype=np.float32)
    grid_w = np.arange(grid_size, dtype=np.float32)
    grid = np.meshgrid(grid_w, grid_h)  # here w goes first
    grid = np.stack(grid, axis=0)

    grid = grid.reshape([2, 1, grid_size, grid_size])
    pos_embed = get_2d_sincos_pos_embed_from_grid(embed_dim, grid)
    if cls_token:
        pos_embed = np.concatenate([np.zeros([1, embed_dim]), pos_embed], axis=0)
    return pos_embed


def get_2d_sincos_pos_embed_from_grid(embed_dim, grid):
    assert embed_dim % 2 == 0

    # use half of dimensions to encode grid_h
    emb_h = get_1d_sincos_pos_embed_from_grid(embed_dim // 2, grid[0])  # (H*W, D/2)
    emb_w = get_1d_sincos_pos_embed_from_grid(embed_dim // 2, grid[1])  # (H*W, D/2)

    emb = np.concatenate([emb_h, emb_w], axis=1) # (H*W, D)
    return emb


def get_1d_sincos_pos_embed_from_grid(embed_dim, pos):
    """
    embed_dim: output dimension for each position
    pos: a list of positions to be encoded: size (M,)
    out: (M, D)
    """
    assert embed_dim % 2 == 0
    omega = np.arange(embed_dim // 2, dtype=np.float32)
    omega /= embed_dim / 2.
    omega = 1. / 10000**omega  # (D/2,)

    pos = pos.reshape(-1)  # (M,)
    out = np.einsum('m,d->md', pos, omega)  # (M, D/2), outer product

    emb_sin = np.sin(out) # (M, D/2)
    emb_cos = np.cos(out) # (M, D/2)

    emb = np.concatenate([emb_sin, emb_cos], axis=1)  # (M, D)
    return emb


# --------------------------------------------------------
# Interpolate position embeddings for high-resolution
# References:
# DeiT: https://github.com/facebookresearch/deit
# --------------------------------------------------------
def interpolate_pos_embed(model, checkpoint_model):
    interpolate_flag = False
    if 'pos_embed' in checkpoint_model:
        pos_embed_checkpoint = checkpoint_model['pos_embed']
        pos_embed_name = 'pos_embed'
        interpolate_flag = True
    elif 'pos_embed_spatial' in checkpoint_model:
        pos_embed_checkpoint = checkpoint_model['pos_embed_spatial']
        pos_embed_name = 'pos_embed_spatial'
        interpolate_flag = True
    if interpolate_flag:
        embedding_size = pos_embed_checkpoint.shape[-1]
        if pos_embed_name == 'pos_embed':
            num_patches = model.patch_embed.num_patches
            num_extra_tokens = model.pos_embed.shape[-2] - num_patches
            # print(f"num_patches: {num_patches}, num_extra_tokens: {num_extra_tokens}")
        elif pos_embed_name == 'pos_embed_spatial':
            num_patches = model.patch_embed.num_patches // (model.patch_embed.frames // model.patch_embed.t_patch_size)
            num_extra_tokens = model.pos_embed_spatial.shape[-2] - num_patches
        # height (== width) for the checkpoint position embedding
        orig_size = int((pos_embed_checkpoint.shape[-2] - num_extra_tokens) ** 0.5)
        # height (== width) for the new position embedding
        new_size = int(num_patches ** 0.5)
        # class_token and dist_token are kept unchanged
        if orig_size != new_size:
            print(f"Position interpolate {pos_embed_name}" + " from %dx%d to %dx%d" % (orig_size, orig_size, new_size, new_size))
            extra_tokens = pos_embed_checkpoint[:, :num_extra_tokens]
            # only the position tokens are interpolated
            pos_tokens = pos_embed_checkpoint[:, num_extra_tokens:]
            pos_tokens = pos_tokens.reshape(-1, orig_size, orig_size, embedding_size).permute(0, 3, 1, 2)
            pos_tokens = torch.nn.functional.interpolate(
                pos_tokens, size=(new_size, new_size), mode='bicubic', align_corners=False)
            pos_tokens = pos_tokens.permute(0, 2, 3, 1).flatten(1, 2)
            new_pos_embed = torch.cat((extra_tokens, pos_tokens), dim=1)
            checkpoint_model[pos_embed_name] = new_pos_embed


# added by zucks
def interpolate_temporal_pos_embed(model, checkpoint_model, smaller_interpolate_type='interp'):
    # assume model is vit for downstream tasks
    # [TODO]: assume no extra tokens, if needed, need to add
    if "pos_embed_temporal" in checkpoint_model:
        pos_embed_checkpoint = checkpoint_model["pos_embed_temporal"]
        embedding_size = pos_embed_checkpoint.shape[-1]
        orig_num_temporal_patches = pos_embed_checkpoint.shape[-2]
        new_num_temporal_patches = model.patch_embed.frames // model.patch_embed.t_patch_size
        if orig_num_temporal_patches != new_num_temporal_patches:
            print(
                "Position interpolate from %d to %d"
                % (orig_num_temporal_patches, new_num_temporal_patches)
            )

            pos_tokens = pos_embed_checkpoint.permute(0, 2, 1)

            if orig_num_temporal_patches > new_num_temporal_patches and smaller_interpolate_type == "crop":
                # crop in the middle
                start_idx = (orig_num_temporal_patches - new_num_temporal_patches) // 2
                pos_tokens = pos_tokens[:, :, start_idx:start_idx + new_num_temporal_patches]
                print(f"Crop in the middle, from {start_idx} to {start_idx + new_num_temporal_patches}")
            else:
                pos_tokens = torch.nn.functional.interpolate(
                    pos_tokens,
                    size=new_num_temporal_patches,
                    mode="linear",
                    align_corners=False,
                )

            pos_tokens = pos_tokens.permute(0, 2, 1)
            new_pos_embed = pos_tokens

            checkpoint_model["pos_embed_temporal"] = new_pos_embed