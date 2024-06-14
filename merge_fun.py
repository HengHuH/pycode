# -*- coding: utf-8 -*-

import sys
import dis
import types
import opcode
import queue

assert sys.version_info.major == 3 and sys.version_info.minor == 11, "for python 3.11 and above"


cache_entries = opcode._inline_cache_entries
backward_jrel = (
    opcode.opmap["JUMP_BACKWARD_NO_INTERRUPT"],
    opcode.opmap["JUMP_BACKWARD"],
    opcode.opmap["POP_JUMP_BACKWARD_IF_NOT_NONE"],
    opcode.opmap["POP_JUMP_BACKWARD_IF_NONE"],
    opcode.opmap["POP_JUMP_BACKWARD_IF_FALSE"],
    opcode.opmap["POP_JUMP_BACKWARD_IF_TRUE"],
)


def merge_func(func_name, funcs, def_argcount=None, debug=1, merged_firstlineno=0):
    print(f"MERGE FUNC:: {func_name}")
    func_info = dict()
    context = {
        "co_names": list(),
        "co_varnames": list(),
        "co_consts": list(),
        "co_freevars": list(),
        "co_cellvars": list(),
        "func_globals": dict(),  # __globals__
        "func_defaults": list(),  # __defaults__
        "func_closure": list(),  # __closure__
        "co_code": bytes(),
        "co_lnotab": None,
        "co_nlocals": 0,
        "co_argcount": 0,
        "co_posonlyargcount": 0,  # * 参数
        "co_kwonlyargcount": 0,
        "co_stacksize": 0,  # virtual machine stack space required
        "co_flags": 0,
        "co_codelen": 0,  # length of bytecode
        "total_cellvars": 0,  # number of cellvars
    }
    merged_code = list()
    for func in funcs:
        context["total_cellvars"] += len(func.__code__.co_cellvars)

    # assert that all functions have the same signature.
    context["co_argcount"] = def_argcount if def_argcount is not None else funcs[0].__code__.co_argcount
    context["co_posonlyargcount"] = funcs[0].__code__.co_posonlyargcount
    context["co_kwonlyargcount"] = funcs[0].__code__.co_kwonlyargcount
    context["co_flags"] = funcs[0].__code__.co_flags
    for func in funcs[1:]:
        assert (
            func.__code__.co_argcount == context["co_argcount"]
            and func.__code__.co_posonlyargcount == context["co_posonlyargcount"]
            and func.__code__.co_kwonlyargcount == context["co_kwonlyargcount"]
            and func.__code__.co_flags == context["co_flags"]
        )

    idx = 0
    for func in funcs:
        data = func_info[idx] = {}
        data["func"] = func
        data["idx"] = idx
        data["code_obj"] = code_obj = func.__code__
        data["co_code"] = code_obj.co_code
        data["co_consts"] = code_obj.co_consts
        data["co_filename"] = code_obj.co_filename
        data["co_lnotab"] = code_obj.co_lnotab
        data["co_name"] = func_name  # function name
        data["co_names"] = code_obj.co_names
        data["co_renames"] = []  # [(old_name, new_name), ]
        data["co_nlocals"] = code_obj.co_nlocals
        data["co_stacksize"] = code_obj.co_stacksize
        data["co_varnames"] = code_obj.co_varnames
        data["same_nlocals"] = 0
        for varname in data["co_varnames"]:
            if varname not in context["co_varnames"]:
                context["co_varnames"].append(varname)
            else:
                data["same_nlocals"] += 1
        data["co_firstlineno"] = code_obj.co_firstlineno
        data["co_cellvars"] = code_obj.co_cellvars
        data["co_freevars"] = code_obj.co_freevars
        data["func_globals"] = func.__globals__
        data["func_defaults"] = func.__defaults__
        data["func_closure"] = func.__closure__

        code_ori = list(code_obj.co_code)
        # cut tail which is not last function.
        is_last = idx == len(funcs) - 1
        if not is_last:
            cl = len(code_ori)
            tl = 0  # length of tail to cut
            if (
                len(code_ori) >= 4
                and code_ori[-4] == opcode.opmap["LOAD_CONST"]
                and code_ori[-2] == opcode.opmap["RETURN_VALUE"]
            ):
                tl = 4
            elif code_ori[-2] in (opcode.opmap["RETURN_VALUE"],):
                tl = 2
            cl = len(code_ori) - tl
        else:  # is_last
            cl = len(code_ori)

        data["co_codelen"] = cl

        # convert opcode
        tmpcodes = []
        inserts = queue.Queue()
        jumps = []  # record jump opcode
        i = 0
        while i < cl:
            bc = code_ori[i]
            if bc >= opcode.HAVE_ARGUMENT:  # 有参 opcode
                if bc == opcode.opmap["RESUME"] and idx > 0:
                    # replace RESUME with NOP, NOP
                    tmpcodes.extend([opcode.opmap["NOP"]] * 2)
                    i += 2
                else:
                    pi = i
                    while bc == opcode.EXTENDED_ARG:
                        pi += 2
                        bc = code_ori[pi]
                    if bc in opcode.hasjrel:
                        jumps.append((i, len(tmpcodes), pi + 2 - i))
                    try:
                        handler = REGISTER_HANDLES[bc]
                    except:
                        raise Exception(f"opcode [{bc}]:{opcode.opname[bc]} dont have converter.")
                    opbytes = code_ori[i: pi + 2]
                    result, inserted = handler(opbytes, context, data)
                    tmpcodes.extend(result)
                    while inserted > 0:
                        inserts.put(i)
                        inserted -= 1
                    i = pi + 2
            elif bc == opcode.opmap["RETURN_VALUE"] and not is_last:
                # RETURN_VALUE 替换，防止函数提前结束
                # repace return opcode in the middle of the not last function to avoid ending early
                prev_bc = code_ori[i - 2]
                if prev_bc == opcode.opmap["LOAD_CONST"]:
                    tmpcodes = tmpcodes[:-2]
                    codes = make_jump_forward((cl - i) // 2)
                    jumps.append((i, len(tmpcodes), len(codes)))

                    tmpcodes.extend(codes)
                    if len(codes) == 2:
                        tmpcodes.extend([opcode.opmap["NOP"]] * 2)
                    else:
                        inserted = (len(codes) - 4) // 2
                    iat = i - 2
                else:
                    codes = make_jump_forward((cl - i) // 2)
                    inserted = (len(codes) - 2) // 2
                    tmpcodes.extend(codes)
                    jumps.append((i, len(tmpcodes), len(codes)))
                    iat = i
                while inserted > 0:
                    inserts.put(iat)
                    inserted -= 1
                i += 2
            else:  # 其他无参 opcode
                tmpcodes.extend(code_ori[i : i + 2])
                i += 2
            if cache_entries[bc] > 0:
                tmpcodes.extend(code_ori[i : i + cache_entries[bc] * 2])
                i += cache_entries[bc] * 2

        # after convert, deal with relative jump opcode.
        while not inserts.empty():
            iat = inserts.get()
            for jump in jumps:
                jcodes = code_ori[jump[0]: jump[2]]
                jarg = 0
                for i in range(-1, -len(jcodes), -2):
                    jarg |= opbytes[i] << (abs(i) // 2 * 8)
                jop = jcodes[-2]
                cover = True
                if jop not in backward_jrel:
                    cover = jump[0] < iat <= jump[0] + jarg * 2
                else:
                    cover = jump[0] - jarg * 2 <= iat < jump[0]
                if cover:
                    jarg += 1
                cvt = []
                while jarg > 0:
                    word = jarg & 0xFF
                    jarg = jarg >> 8
                    if cvt:
                        cvt = [opcode.EXTENDED_ARG, word] + cvt
                    else:
                        cvt = [jop, word]
                if len(cvt) > jump[2]:
                    inserts.put(jump[0])
                tmpcodes = tmpcodes[0 : jump[1]] + cvt + tmpcodes[jump[1] + jump[2] : -1]

        # merge to context.
        merged_code += tmpcodes
        context["co_names"].extend(data["co_names"])
        if data["co_renames"]:
            context["co_names"].extend(e[1] for e in data["co_renames"])  # extend new names
        context["co_consts"].extend(data["co_consts"])
        context["co_cellvars"].extend(data["co_cellvars"])
        context["co_freevars"].extend(data["co_freevars"])
        context["co_nlocals"] += data["co_nlocals"] - data["same_nlocals"]
        context["co_stacksize"] = max(data["co_stacksize"], context["co_stacksize"])
        if merged_code:
            context["co_codelen"] = len(merged_code)
        else:
            context["co_codelen"] = 0

        # fix func globals has same key but different value
        if data["func_globals"]:
            renames = {e[0]: e[1] for e in data["co_renames"]}
            for k, v in data["func_globals"].items():
                if k in renames:
                    context["func_globals"][renames[k]] = v
                elif k not in context["func_globals"]:
                    context["func_globals"][k] = v

        if data["func_defaults"]:
            context["func_defaults"].extend(data["func_defaults"])
        if data["func_closure"]:
            context["func_closure"].extend(data["func_closure"])
        idx += 1

    # generate merged function.
    context["co_code"] = bytes(merged_code)

    for k, v in context.items():
        if type(v) is list:
            context[k] = tuple(v)

    mycode_obj = types.CodeType(
        context["co_argcount"],  # number of arguments (not including * or ** args)
        context["co_posonlyargcount"],  # int, 函数的仅限位置 形参 的总数（包括具有默认值的参数）
        context["co_kwonlyargcount"],  # int, 函数的仅限关键字 形参 的数量（包括具有默认值的参数
        context["co_nlocals"],  # number of local varialbes
        context["co_stacksize"] + 10,  # int, 取max_stacksize+1, +1 是为了避免内存crash
        context["co_flags"],  # bitmap: 1=optimized | 2=newlocals | 4=*arg | 8=**arg
        context["co_code"],  # bytes of raw compiled bytecode
        context["co_consts"],  # tuple of constants used in the bytecode
        context["co_names"],  # tuple of names of local variables
        context["co_varnames"],  # tuple of names of arguments and local variables
        "merge_funcion_generated.py",  # filename
        func_name,  # str, function name
        "",  # qualname
        merged_firstlineno,  # 需要能设置firstlino,否则tracy会无法正确识别函数名
        bytes(),  # encoded mapping of line numbers to bytecode indices
        bytes(),  # exceptiontable
        context["co_freevars"],  # the names of the free variables.
        context["co_cellvars"],  # the names of the local variables that are referenced by nested functions.
    )
    func_generated = types.FunctionType(
        mycode_obj,  # code object
        context.get("func_globals", {}),  # __globals__
        func_name,  # __name__
        context.get("func_defaults", ()),  # __default__
        context.get("func_closure", ()),  # __closure__
    )
    return func_generated


def convert_co_names(opbytes, context, data):
    """由于合并了co_names,全局变量读取的位置变更"""
    _arg = 0
    _byte = opbytes[-2]
    for i in range(-1, -len(opbytes), -2):
        _arg |= opbytes[i] << (abs(i) // 2 * 8)

    offset = len(context.get("co_names"))
    _arg += offset

    res = []
    if _arg == 0:
        res = [_byte, 0]
    while _arg > 0:
        word = _arg & 0xFF
        _arg = _arg >> 8
        if res:
            res = [opcode.EXTENDED_ARG, word] + res
        else:
            res = [_byte, word]
    return res, (len(res) - len(opbytes)) // 2


def convert_co_renames(opbytes, context, data):
    """由于合并了func_globals, co_names, 全局变量读取的位置变更，重命名"""
    _arg = 0
    _byte = opbytes[-2]
    for i in range(-1, -len(opbytes), -2):
        _arg |= opbytes[i] << (abs(i) // 2 * 8)

    offset = len(context.get("co_names"))
    has_null = _arg & 0x01
    if _byte == opcode.opmap["LOAD_GLOBAL"]:
        namei = _arg >> 1
    else:
        namei = _arg

    name = data["co_names"][namei]
    if (
        name in context.get("func_globals")
        and name in data.get("func_globals")
        and context.get("func_globals")[name] != data.get("func_globals")[name]
    ):
        newname = "%s_%s" % (name, data["idx"])
        data.get("co_renames").append((name, newname))
        offset = len(context.get("co_names")) + len(data.get("co_names")) + len(data.get("co_renames")) - 1
        current = offset
    else:
        offset = len(context.get("co_names"))
        current = namei + offset

    if _byte == opcode.opmap["LOAD_GLOBAL"]:
        namei = (current << 1) | (0x01 if has_null else 0x00)
    else:
        namei = current

    _arg = namei
    res = []
    if _arg == 0:
        res = [_byte, 0]
    while _arg > 0:
        word = _arg & 0xFF
        _arg = _arg >> 8
        if res:
            res = [opcode.EXTENDED_ARG, word] + res
        else:
            res = [_byte, word]

    return res, (len(res) - len(opbytes)) // 2


def convert_co_consts(opbytes, context, data):
    """由于合并了co_consts,常量读取的位置变更"""
    _arg = 0
    _byte = opbytes[-2]
    for i in range(-1, -len(opbytes), -2):
        _arg |= opbytes[i] << (abs(i) // 2 * 8)

    offset = len(context.get("co_consts"))
    _arg += offset

    res = []
    if _arg == 0:
        res = [_byte, 0]
    while _arg > 0:
        word = _arg & 0xFF
        _arg = _arg >> 8
        if res:
            res = [opcode.EXTENDED_ARG, word] + res
        else:
            res = [_byte, word]
    return res, (len(res) - len(opbytes)) // 2


def convert_varnames(opbytes, context, data):
    """由于合并了co_varnames,局部变量读取的位置变更"""
    _arg = 0
    _byte = opbytes[-2]
    for i in range(-1, -len(opbytes), -2):
        _arg |= opbytes[i] << (abs(i) // 2 * 8)

    if data["co_varnames"][_arg] in context.get("co_varnames"):
        current = context.get("co_varnames").index(data["co_varnames"][_arg])
    else:
        current = len(context.get("co_varnames"))
        context.get("co_varnames").append(data.get("co_varnames")[_arg])

    _arg = current
    res = []
    if _arg == 0:
        res = [_byte, 0]
    while _arg > 0:
        word = _arg & 0xFF
        _arg = _arg >> 8
        if res:
            res = [opcode.EXTENDED_ARG, word] + res
        else:
            res = [_byte, word]
    return res, (len(res) - len(opbytes)) // 2


def convert_closure(opbytes, context, data):
    """由于合并了 co_cellvars，cell index 需要变更"""
    _arg = 0
    _byte = opbytes[-2]
    for i in range(-1, -len(opbytes), -2):
        _arg |= opbytes[i] << (abs(i) // 2 * 8)

    offset = len(context.get("co_cellvars"))
    _arg += offset

    res = []
    ext = 0
    if _arg == 0:
        res = [_byte, 0]
    while _arg > 0:
        word = _arg & 0xFF
        _arg = _arg >> 8
        if res:
            res = [opcode.EXTENDED_ARG, word] + res
            ext += 1
        else:
            res = [_byte, word]

    return res, ext


def convert_default(opbytes, context, data):
    """参数不做任何变化"""
    return opbytes, 0


def make_jump_forward(delta):
    _byte = opcode.opmap["JUMP_FORWARD"]
    _arg = delta

    res = []
    if _arg == 0:
        res = [_byte, 0]
    while _arg > 0:
        word = _arg & 0xFF
        _arg = _arg >> 8
        if res:
            res = [opcode.EXTENDED_ARG, word] + res
        else:
            res = [_byte, word]

    return res


REGISTER_HANDLES = {
    # convert names
    opcode.opmap["STORE_NAME"]: convert_co_names,
    opcode.opmap["DELETE_NAME"]: convert_co_names,
    opcode.opmap["STORE_ATTR"]: convert_co_names,
    opcode.opmap["DELETE_ATTR"]: convert_co_names,
    opcode.opmap["LOAD_NAME"]: convert_co_names,
    opcode.opmap["LOAD_ATTR"]: convert_co_names,
    opcode.opmap["IMPORT_NAME"]: convert_co_names,
    opcode.opmap["IMPORT_FROM"]: convert_co_names,
    opcode.opmap["LOAD_METHOD"]: convert_co_names,
    # convert var names
    opcode.opmap["LOAD_FAST"]: convert_varnames,
    opcode.opmap["STORE_FAST"]: convert_varnames,
    opcode.opmap["DELETE_FAST"]: convert_varnames,
    # convert renames
    opcode.opmap["STORE_GLOBAL"]: convert_co_renames,
    opcode.opmap["DELETE_GLOBAL"]: convert_co_renames,
    opcode.opmap["LOAD_GLOBAL"]: convert_co_renames,
    # convert consts
    opcode.opmap["LOAD_CONST"]: convert_co_consts,
    opcode.opmap["KW_NAMES"]: convert_co_consts,
    # convert default
    opcode.opmap["UNPACK_SEQUENCE"]: convert_default,
    opcode.opmap["UNPACK_EX"]: convert_default,
    opcode.opmap["SWAP"]: convert_default,
    opcode.opmap["BUILD_TUPLE"]: convert_default,
    opcode.opmap["BUILD_LIST"]: convert_default,
    opcode.opmap["BUILD_SET"]: convert_default,
    opcode.opmap["BUILD_MAP"]: convert_default,
    opcode.opmap["COMPARE_OP"]: convert_default,
    opcode.opmap["IS_OP"]: convert_default,
    opcode.opmap["CONTAINS_OP"]: convert_default,
    opcode.opmap["RERAISE"]: convert_default,
    opcode.opmap["COPY"]: convert_default,
    opcode.opmap["BINARY_OP"]: convert_default,
    opcode.opmap["RAISE_VARARGS"]: convert_default,
    opcode.opmap["GET_AWAITABLE"]: convert_default,
    opcode.opmap["MAKE_FUNCTION"]: convert_default,
    opcode.opmap["BUILD_SLICE"]: convert_default,
    opcode.opmap["MAKE_CELL"]: convert_default,
    opcode.opmap["CALL_FUNCTION_EX"]: convert_default,
    # opcode.opmap["EXTENDED_ARG"]: convert_default,
    opcode.opmap["LIST_APPEND"]: convert_default,
    opcode.opmap["SET_ADD"]: convert_default,
    opcode.opmap["MAP_ADD"]: convert_default,
    opcode.opmap["RESUME"]: convert_default,
    opcode.opmap["MATCH_CLASS"]: convert_default,
    opcode.opmap["FORMAT_VALUE"]: convert_default,
    opcode.opmap["BUILD_CONST_KEY_MAP"]: convert_default,
    opcode.opmap["BUILD_STRING"]: convert_default,
    opcode.opmap["LIST_EXTEND"]: convert_default,
    opcode.opmap["SET_UPDATE"]: convert_default,
    opcode.opmap["DICT_MERGE"]: convert_default,
    opcode.opmap["DICT_UPDATE"]: convert_default,
    opcode.opmap["PRECALL"]: convert_default,
    opcode.opmap["CALL"]: convert_default,
    # convert relative jump
    opcode.opmap["FOR_ITER"]: convert_default,
    opcode.opmap["JUMP_FORWARD"]: convert_default,
    opcode.opmap["POP_JUMP_FORWARD_IF_FALSE"]: convert_default,
    opcode.opmap["POP_JUMP_FORWARD_IF_TRUE"]: convert_default,
    opcode.opmap["SEND"]: convert_default,
    opcode.opmap["POP_JUMP_FORWARD_IF_NOT_NONE"]: convert_default,
    opcode.opmap["POP_JUMP_FORWARD_IF_NONE"]: convert_default,
    opcode.opmap["JUMP_BACKWARD_NO_INTERRUPT"]: convert_default,
    opcode.opmap["JUMP_BACKWARD"]: convert_default,
    opcode.opmap["POP_JUMP_BACKWARD_IF_NOT_NONE"]: convert_default,
    opcode.opmap["POP_JUMP_BACKWARD_IF_NONE"]: convert_default,
    opcode.opmap["POP_JUMP_BACKWARD_IF_FALSE"]: convert_default,
    opcode.opmap["POP_JUMP_BACKWARD_IF_TRUE"]: convert_default,
    opcode.opmap["JUMP_IF_FALSE_OR_POP"]: convert_default,
    opcode.opmap["JUMP_IF_TRUE_OR_POP"]: convert_default,
    # convert closure
    opcode.opmap["LOAD_CLOSURE"]: convert_closure,
    opcode.opmap["LOAD_DEREF"]: convert_closure,
    opcode.opmap["STORE_DEREF"]: convert_closure,
    opcode.opmap["DELETE_DEREF"]: convert_closure,
    opcode.opmap["LOAD_CLASSDEREF"]: convert_closure,
    opcode.opmap["COPY_FREE_VARS"]: convert_closure,
}

if __name__ == "__main__":

    def f1(dt):
        print(min(1, 2))
        a = 1
        if a:
            return 1
        if 1 + 2:
            print(1, dt)
            return 2
        else:
            print(3)
            return 4

    def f2(dt):
        print(min(2, 3))
        print(2, dt)

    def f3(dt):
        print(3, dt)

    def f4(dt):
        print(4, dt)

    merge_list = [f1, f2, f3, f4]
    for f in merge_list:
        print(f"===== {f.__name__} =====")
        dis.dis(f)
    f_merged = merge_func("f_merge", merge_list)
    dis.dis(f_merged.__code__)

    print("---")
    for f in merge_list:
        f("hi")
    print("---")
    f_merged("hi")
