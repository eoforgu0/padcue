idf_component_register(
    SRCS "padctl_core.c" "padctl_procon.c" "padctl_hidpad.c"
         "padctl_usb_desc.c" "padctl_usb_desc_hidpad.c" "padctl_tx.c"
    INCLUDE_DIRS "include"
    LDFRAGMENTS "linker.lf"
)
