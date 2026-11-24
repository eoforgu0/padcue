idf_component_register(
    SRCS "pademu_core.c" "pademu_procon.c"
         "pademu_usb_desc.c" "pademu_tx.c"
    INCLUDE_DIRS "include"
    LDFRAGMENTS "linker.lf"
)

# 純粋 C・IDF 非依存・小規模なので、ホストのテスト(gcc -Wall -Werror)と
# 同じ厳しさで通す。ここが緩いと、ホストではエラーになる書き方が実機側の
# ビルドだけ通ってしまう
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
