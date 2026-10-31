# UniXNU kernel components (BSD-2-Clause-Patent)

Copied unchanged from UniXNU (each directory keeps its LICENSE and SPDX header);
`apply.sh` copies the sources into `iokit/Kernel/arm/` of the patched xnu tree and
`patches/fuxi/0004-components.patch` lists them in `iokit/conf/files.arm64`, so
they are compiled into the kernel instead of linked as UniXNU's `lib*.a`.

| Component | Upstream | Revision | sha256 of the .cpp |
|---|---|---|---|
| ArmCpu (GICv3, generic timer, PSCI CPU start) | https://github.com/UniXNU/ArmCpu | 4bd3230f48121e47a0badb4ea2d1674715ba2197 | 202bc278dfe52c2d0a988ec3672a95fbbd169be7f3b323d088b93c8e814f6f8f |
| ArmDTPlatform (Apple-format DT platform expert) | https://github.com/UniXNU/ArmDTPlatform | 417ff95d6a6e8607ddea9309ec45c3bfd4a67c60 | 07f96a42bdd638022960f5fcaf9e3a07dd73a82c869a70815781cac6d4873b3b |
