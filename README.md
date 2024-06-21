# pycode
merge python functions

## Closure

co_varnames  参数名 + 局部变量名（非 cell）
co_cellnames cell化的参数 + 局部变量名

被闭包捕获的局部变量会 cell 化。

MAKE_CELL(i)
LOAD_CLOSURE(i)
LOAD_DEREF(i)
STORE_DEREF(i)
DELETE_DEREF(i)
<!-- LOAD_CLASSDEREF -->

以上相关指令的索引 i 的含义为，free locals 的槽位。

MAKE_CELL 一般放在OPCODE顶部，RESUME 前。放在函数中部，也可以生效。只是之后对局部变量的操作需要使用 DEREF 系列的指令。

1. 解决 closure 的索引问题
2. 修正 MAKE_CELL 后，局部变量的操作指令要改为 DEREF
