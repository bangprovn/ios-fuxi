ENTRY(_head)
SECTIONS {
 . = 0;
 .text : { *(.text.head) *(.text*) }
 .rodata : { *(.rodata*) }
 .gpu_fw : { *(.gpu_fw) }
 .data : { *(.data*) }
 . = ALIGN(16384);
 _bss_start = .;
 .bss : { *(.bss*) *(COMMON) }
 . = ALIGN(16);
 _bss_end = .;
 . += 0x8000;
 _stack_top = .;
 . += 0x8000;
 _secondary_stack_top = .;
 _payload_start = .;
 .payload : { *(.payload) }
 _payload_end = .;
 .adsp_fw : { *(.adsp_fw) }
 _image_end = .;
 _end = .;
 /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) }
}
