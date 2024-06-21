# Python 函数合并

合并函数，降低函数调用成本。仅支持 Python 3.11。

## 背景

继续下去前，需要了解一些前置知识：

- 对Python的编译和解释，函数栈帧的创建和执行过程有基础概念
- types.FunctionType 函数类型
- types.CodeType 代码类型
- [dis](https://docs.python.org/zh-cn/3.11/library/dis.html) 模块
- [The bytecode interpreter of Python3.11]https://devguide.python.org/internals/interpreter/#jumps

## 可行性

types.FunctionType 的实例含有函数在运行时需要的所有信息。将多个函数合并，也就是把它们的函数对象以一定规则合并，使用合并后的数据，新建 types.FunctionType 实例，创建出一个新的函数实例。需要合并的属性有：

- \_\_code\_\_
- \_\_globals\_\_
- \_\_defaults\_\_
- \_\_closure\_\_

具体的合并细节在下一节详细讨论。

## 合并函数

### 合并全局环境

\_\_globals\_\_ 全局环境的字典。合并策略：key,value都相同，排重；当有相同key，但不同value时，重命名 key。

### 合并参数环境

\_\_defaults\_\_，默认参数元组。合并策略：依次连接，不排重。

### 合并闭包

\_\_closure\_\_， 胞体元组。合并策略：依次连接，不排重。

## 合并 \_\_code\_\_

### 变量名合并

co_names， 代码内的所有变量名。不排重，直接合并，另外还要增加合并 globals 时造成的重命名名字。

### 常量合并

co_consts，常量元组，直接合并。

### 字节码合并

合并，修正 co_code, co_codelen

重要的变化

1. 3.6 变更，每条指令使用 2 个字节，使用前置的 EXTENDED_ARG(ext) 支持超过一个字节的数据，最多允许三个。
2. 3.10 变更：跳转、异常处理和循环指令的参数为指令偏移量，而不是字节偏移量
3. 3.11 变更：有些指令带有一个或多个CACHE指令
4. LOAD_GLOBAL(namei)，如果设置了 namei 的最低位，则会在全局变量前，将一个 NULL 推入栈
5. 异常处理增加了 co_exceptiontable ，
6. 生成式使用闭包实现
7. 闭包和CELL相关的指令，参数不再是 co_varnames 的长度偏移量，而是 "fast locals" 存储的 i 号槽位。

合并指令时，原则是不删指令，尽量替换为 NOP，或者增加指令。

#### 无参指令

- CACHE: 每个指令拥有的 CACHE 数的数据在 opcode.py _inline_cache_entries 中，直接合并。
- RESUME: 只是一个标志，可以直接合入。
- RETURN_VALUE: 对于不是最后一个函数，如果在函数尾部，替换为 NOP；对于所有函数，如果在中部，替换为 JUMP_FORWARD(delta)，跳到下一个函数头部，如果前个指令是 LOAD_CONST(namei)，则把它替换为 NOP。
- 默认合并其他无参指令。

#### 操作数

对于有参数的指令，在合并时，操作数可能发生变化。当参数超出原操作数的上限时，需要插入 EXTENDED_ARG。

#### 跳转

考虑到有参指令的修改可能插入指令，如果插入位置被跳转范围覆盖，需要增加跳转指令。需要注意，增加跳转指令的参数，可能造成新的 EXTENDED_ARG 指令插入。

#### 异常处理

处理 co_exceptiontable， 合并所有 entry, 因为有插入指令，需要修正其中的数据。

#### 闭包

因为函数的局部变量，可能在别的函数中会被 MAKE_CELL(i) 指令变为 CELL。面对这种情况，做以下修改：

- 在合并所有函数前，先合并 co_varnames, co_cellvars
- 使用合并后的 co_varnames，co_cellvars 生成新的 MAKE_CELL(i) 放在合并函数的最前面
- 将原函数的 MAKE_CELL(i) 替换为 (NOP, 0)
- 修正 closure 相关的指令的操作数
- 修正 XXXX_FAST 相关指令的操作数
- 如果 XXXX_FAST 指向的变量被 CELL 化了，修改操作码为 XXXX_DEREF
- 异常处理的合并需要考虑 MAKE_CELL(i) 的插入

### 栈帧大小

简单取最大值可能会造成溢出，因为不太方便计算，现在只是取最大再 +10.
