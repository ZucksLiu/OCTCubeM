
# Copyright (c) Zixuan Liu et al, OCTCubeM group
# All rights reserved.

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.


from pathlib import Path

from main_pretrain_oph_joint_2d512_flash_attn import get_args_parser, main


def invoke_main() -> None:
    args = get_args_parser()
    args = args.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)


if __name__ == "__main__":
    invoke_main()  # pragma: no cover
