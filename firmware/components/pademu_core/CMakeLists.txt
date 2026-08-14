idf_component_register(
    SRCS "pademu_core.c" "pademu_procon.c" "pademu_hidpad.c"
         "pademu_usb_desc.c" "pademu_usb_desc_hidpad.c" "pademu_tx.c"
    INCLUDE_DIRS "include"
    LDFRAGMENTS "linker.lf"
)
