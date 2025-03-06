# Copyright (c) Zixuan Liu et al, OCTCubeM group
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# DeiT: https://github.com/facebookresearch/deit
# BEiT: https://github.com/microsoft/unilm/tree/master/beit
# MAE: https://github.com/facebookresearch/mae/tree/main
# MAE_ST: https://github.com/facebookresearch/mae_st
# --------------------------------------------------------


OUTPUTDIR=YOUR_OUTPUT_DIR
kermany_data_dir=YOUR_KERMANY_DATA_DIR
BSZ=1
INPUTSIZE=256
ACCUMSTEPS=1
EPOCHS=50
BLR=1.6e-3
RATIO=0.9
resume_epoch=9

SCHEDULER="bsz-$BSZ-inputsize-$INPUTSIZE-aacumsteps$ACCUMSTEPS-ep-$EPOCHS-lr-$BLR-test-512-flash-attn"
OUTPUTDIR=$OUTPUTDIR/$SCHEDULER

CUDA_VISIBLE_DEVICES=0,1,2,3 torchrun --nproc_per_node=4 --master_port=25680 run_pretrain_oph_joint_test_512_flash_attn.py \
        --kermany_data_dir $kermany_data_dir \
        --output_dir $OUTPUTDIR \
        --log_dir $OUTPUTDIR/log_dir \
        --batch_size $BSZ \
        --accum_iter $ACCUMSTEPS \
        --epochs $EPOCHS \
        --blr $BLR \
        --mask_ratio $RATIO \
        --weight_decay 0.05 \
        --num_workers 24 \
        --num_frames 60 \
        --t_patch_size 3 \
        --pred_t_dim 60 \
        --input_size $INPUTSIZE \
        --warmup_epochs 1 \
        --resume ${OUTPUTDIR}/checkpoint-0000${resume_epoch}.pth \
        --resume_type training_latest \
        --model flash_attn_mae_vit_large_patch16 \
        --batch_size_2d 64 \
        --mask_ratio_2d_min 0.75 \
        --mask_ratio_2d_max 0.85 \
        --K_min 0.15 \
        --K_max 0.3 \
        --epoch_offset 0 \
        --high_res_input_size 512 \
        # --eval_only
        # --epoch_load_spl 17 \
        # --load_spl_dir $CKPTDIR \

