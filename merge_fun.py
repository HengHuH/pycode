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


def _parse_varint(iterator):
    b = next(iterator)
    val = b & 63
    while b&64:
        val <<= 6
        b = next(iterator)
        val |= b&63
    return val


def parse_exception_table(code):
    iterator = iter(code.co_exceptiontable)
    entries = []
    try:
        while True:
            start = _parse_varint(iterator) * 2
            length = _parse_varint(iterator) * 2
            end = start + length
            target = _parse_varint(iterator) * 2
            dl = _parse_varint(iterator)
            entries.append([start, end, target, dl])
    except StopIteration:
        return entries


def _write_varint(bytes, val):
    res = []
    res.append(val&63)
    val >>= 6
    while val > 0:
        res.append(64|val&63)
        val >>= 6
    bytes += reversed(res)


def write_exception_table(enties):
    res = bytes()
    for entry in enties:
        arr = []
        _write_varint(arr, entry[0] // 2)  # start
        _write_varint(arr, (entry[1] - entry[0]) // 2)  # length
        _write_varint(arr, entry[2] // 2)  # target
        _write_varint(arr, entry[3])  # depth,lasti
        res += bytes(arr)
    return res


def merge_func(func_name, funcs, def_argcount=None, debug=1, merged_firstlineno=0):
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
        "co_linetable": bytes(),
        "co_exceptiontable": bytes(),
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

    for idx, func in enumerate(funcs):
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
        data['co_linetable'] = code_obj.co_linetable
        data["co_exceptiontable"] = parse_exception_table(code_obj)
        data["func_globals"] = func.__globals__
        data["func_defaults"] = func.__defaults__
        data["func_closure"] = func.__closure__

        code_ori = list(code_obj.co_code)
        # cut tail which is not last function.
        is_last = idx == len(funcs) - 1
        cl = data["co_codelen"] = len(code_ori)

        # convert opcode
        tmpcodes = []
        inserts = queue.Queue()
        jumps = []  # record jump opcode
        i = 0
        while i < cl:
            bc = code_ori[i]
            if bc >= opcode.HAVE_ARGUMENT:  # 有参 opcode
                if bc == opcode.opmap["RESUME"] and idx > 0:
                    # replace RESUME with (NOP, NOP) in not first function.
                    tmpcodes.extend([opcode.opmap["NOP"]] * 2)
                    i += 2
                else:
                    pi = i
                    while bc == opcode.EXTENDED_ARG:
                        pi += 2
                        bc = code_ori[pi]
                    if bc in opcode.hasjrel:
                        jumps.append([i, len(tmpcodes), pi + 2 - i, 0])
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
                if i < len(code_ori) - 2:
                    # repace return opcode in the middle of the not last function to avoid ending early
                    prev_bc = code_ori[i - 2]
                    if prev_bc == opcode.opmap["LOAD_CONST"]:
                        tmpcodes = tmpcodes[:-2]
                        codes = make_jump_forward((cl - i) // 2)
                        jumps.append([i, len(tmpcodes), len(codes), 0])
                        tmpcodes.extend(codes)
                        if len(codes) == 2:
                            tmpcodes.extend([opcode.opmap["NOP"]] * 2)
                        else:
                            inserted = (len(codes) - 4) // 2
                        iat = i - 2
                    else:
                        codes = make_jump_forward((cl - i) // 2)
                        inserted = (len(codes) - 2) // 2
                        jumps.append([i, len(tmpcodes), len(codes), 0])
                        tmpcodes.extend(codes)
                        iat = i
                    while inserted > 0:
                        inserts.put(iat)
                        inserted -= 1
                else:
                    # replace tail RETURN_VALUE with NOP
                    prev_bc = code_ori[i - 2]
                    if prev_bc == opcode.opmap['LOAD_CONST']:
                        tmpcodes = tmpcodes[:-2]
                        tmpcodes.extend([opcode.opmap['NOP']] * 4)
                    else:
                        tmpcodes.extend([opcode.opmap['NOP']] * 2)

                i += 2
            else:  # opcode with no argument.
                tmpcodes.extend(code_ori[i : i + 2])
                i += 2
            # CACHE
            if cache_entries[bc] > 0:
                tmpcodes.extend(code_ori[i : i + cache_entries[bc] * 2])
                i += cache_entries[bc] * 2

        # after converted, deal with relative jump opcode.
        allinserts = []
        while not inserts.empty():
            iat = inserts.get()
            allinserts.append(iat)
            for jump in jumps:
                jcodes = code_ori[jump[0]: jump[0] + jump[2]]
                jarg = 0
                for i in range(-1, -len(jcodes), -2):
                    jarg |= jcodes[i] << (abs(i) // 2 * 8)
                jop = jcodes[-2]
                cover = True
                if jop not in backward_jrel:
                    cover = jump[0] < iat <= jump[0] + jump[2] + jarg * 2
                else:
                    cover = jump[0] - jarg * 2 <= iat < jump[0]

                if not cover:
                    continue

                if jarg == 0xFF or jarg == 0xFFFF or jarg == 0xFFFFFF:
                    inserts.put(jump[0])

                jump[3] = jarg + 1 if jump[3] == 0 else jump[3] + 1

        # reversed order to avoid tmpcode index error.
        for jump in reversed(jumps):
            if not jump[3] == 0:
                jarg = jump[3]
                cvt = []
                jop = code_ori[jump[0]: jump[0] + jump[2]][-2]
                while jarg > 0:
                    word = jarg & 0xFF
                    jarg = jarg >> 8
                    if cvt:
                        cvt = [opcode.EXTENDED_ARG, word] + cvt
                    else:
                        cvt = [jop, word]
                tmpcodes = tmpcodes[0: jump[1]] + cvt + tmpcodes[jump[1] + jump[2]:]

        # deal with exception table.
        if data['co_exceptiontable']:
            exc_deltas = [0, 0, 0] * len(data['co_exceptiontable'])
            for i in range(len(data['co_exceptiontable'])):
                entry = data['co_exceptiontable'][i]
                start = entry[0]
                end = entry[1] - 2
                target = entry[2]
                for isa in allinserts:
                    if isa <= start:
                        exc_deltas[i*3+0] += 2
                        exc_deltas[i*3+1] += 2
                        exc_deltas[i*3+2] += 2

                    if start < isa <= end:
                        exc_deltas[i*3+1] += 2
                        exc_deltas[i*3+2] += 2

                    if end < isa <= target:
                        exc_deltas[i*3+2] += 2

            for i in range(len(data['co_exceptiontable'])):
                entry = data['co_exceptiontable'][i]

                entry[0] = entry[0] + exc_deltas[i*3+0] + context['co_codelen']
                entry[1] = entry[1] + exc_deltas[i*3+1] + context['co_codelen']
                entry[2] = entry[2] + exc_deltas[i*3+2] + context['co_codelen']

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
        context["co_exceptiontable"] += write_exception_table(data["co_exceptiontable"])
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
        context['co_linetable'],  # co_linetable, encoded mapping of line numbers to bytecode indices
        context["co_exceptiontable"],  # co_exceptiontable, bytes
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
        # rename
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


def _debug_func(f):
    print(f"debug {f.__name__}")
    co = f.__code__
    print(f"co_argcount: {co.co_argcount}")
    print(f"co_posonlyargcount: {co.co_posonlyargcount}")
    print(f"co_kwonlyargcount: {co.co_kwonlyargcount}")
    print(f"co_nlocals: {co.co_nlocals}")
    print(f"co_stacksize: {co.co_stacksize}")
    print(f"co_flags: {co.co_flags}")
    print(f"co_consts: {co.co_consts}")
    print(f"co_names: {co.co_names}")
    print(f"co_varnames: {co.co_varnames}")
    print(f"co_exceptiontable: {co.co_exceptiontable}")
    print(f"co_freevars: {co.co_freevars}")
    print(f"co_cellvars: {co.co_cellvars}")

    print("== code ==")
    dis.dis(f)


if __name__ == "__main__":

    class A():
        def __init__(self) -> types.NoneType:
            pass

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
        b = A()
        b.a0 = 0
        b.a1 = 1
        b.a2 = 2
        b.a3 = 3
        b.a4 = 4
        b.a5 = 5
        b.a6 = 6
        b.a7 = 7
        b.a8 = 8
        b.a9 = 9
        b.a10 = 10
        b.a11 = 11
        b.a12 = 12
        b.a13 = 13
        b.a14 = 14
        b.a15 = 15
        b.a16 = 16
        b.a17 = 17
        b.a18 = 18
        b.a19 = 19
        b.a20 = 20
        b.a21 = 21
        b.a22 = 22
        b.a23 = 23
        b.a24 = 24
        b.a25 = 25
        b.a26 = 26
        b.a27 = 27
        b.a28 = 28
        b.a29 = 29
        b.a30 = 30
        b.a31 = 31
        b.a32 = 32
        b.a33 = 33
        b.a34 = 34
        b.a35 = 35
        b.a36 = 36
        b.a37 = 37
        b.a38 = 38
        b.a39 = 39
        b.a40 = 40
        b.a41 = 41
        b.a42 = 42
        b.a43 = 43
        b.a44 = 44
        b.a45 = 45
        b.a46 = 46
        b.a47 = 47
        b.a48 = 48
        b.a49 = 49
        b.a50 = 50
        b.a51 = 51
        b.a52 = 52
        b.a53 = 53
        b.a54 = 54
        b.a55 = 55
        b.a56 = 56
        b.a57 = 57
        b.a58 = 58
        b.a59 = 59
        b.a60 = 60
        b.a61 = 61
        b.a62 = 62
        b.a63 = 63
        b.a64 = 64
        b.a65 = 65
        b.a66 = 66
        b.a67 = 67
        b.a68 = 68
        b.a69 = 69
        b.a70 = 70
        b.a71 = 71
        b.a72 = 72
        b.a73 = 73
        b.a74 = 74
        b.a75 = 75
        b.a76 = 76
        b.a77 = 77
        b.a78 = 78
        b.a79 = 79
        b.a80 = 80
        b.a81 = 81
        b.a82 = 82
        b.a83 = 83
        b.a84 = 84
        b.a85 = 85
        b.a86 = 86
        b.a87 = 87
        b.a88 = 88
        b.a89 = 89
        b.a90 = 90
        b.a91 = 91
        b.a92 = 92
        b.a93 = 93
        b.a94 = 94
        b.a95 = 95
        b.a96 = 96
        b.a97 = 97
        b.a98 = 98
        b.a99 = 99
        b.a100 = 100
        b.a101 = 101
        b.a102 = 102
        b.a103 = 103
        b.a104 = 104
        b.a105 = 105
        b.a106 = 106
        b.a107 = 107
        b.a108 = 108
        b.a109 = 109
        b.a110 = 110
        b.a111 = 111
        b.a112 = 112
        b.a113 = 113
        b.a114 = 114
        b.a115 = 115
        b.a116 = 116
        b.a117 = 117
        b.a118 = 118
        b.a119 = 119
        b.a120 = 120
        b.a121 = 121
        b.a122 = 122
        b.a123 = 123
        b.a124 = 124
        b.a125 = 125
        b.a126 = 126
        b.a127 = 127
        b.a128 = 128
        b.a129 = 129
        b.a130 = 130
        b.a131 = 131
        b.a132 = 132
        b.a133 = 133
        b.a134 = 134
        b.a135 = 135
        b.a136 = 136
        b.a137 = 137
        b.a138 = 138
        b.a139 = 139
        b.a140 = 140
        b.a141 = 141
        b.a142 = 142
        b.a143 = 143
        b.a144 = 144
        b.a145 = 145
        b.a146 = 146
        b.a147 = 147
        b.a148 = 148
        b.a149 = 149
        b.a150 = 150
        b.a151 = 151
        b.a152 = 152
        b.a153 = 153
        b.a154 = 154
        b.a155 = 155
        b.a156 = 156
        b.a157 = 157
        b.a158 = 158
        b.a159 = 159
        b.a160 = 160
        b.a161 = 161
        b.a162 = 162
        b.a163 = 163
        b.a164 = 164
        b.a165 = 165
        b.a166 = 166
        b.a167 = 167
        b.a168 = 168
        b.a169 = 169
        b.a170 = 170
        b.a171 = 171
        b.a172 = 172
        b.a173 = 173
        b.a174 = 174
        b.a175 = 175
        b.a176 = 176
        b.a177 = 177
        b.a178 = 178
        b.a179 = 179
        b.a180 = 180
        b.a181 = 181
        b.a182 = 182
        b.a183 = 183
        b.a184 = 184
        b.a185 = 185
        b.a186 = 186
        b.a187 = 187
        b.a188 = 188
        b.a189 = 189
        b.a190 = 190
        b.a191 = 191
        b.a192 = 192
        b.a193 = 193
        b.a194 = 194
        b.a195 = 195
        b.a196 = 196
        b.a197 = 197
        b.a198 = 198
        b.a199 = 199
        b.a200 = 200
        b.a201 = 201
        b.a202 = 202
        b.a203 = 203
        b.a204 = 204
        b.a205 = 205
        b.a206 = 206
        b.a207 = 207
        b.a208 = 208
        b.a209 = 209
        b.a210 = 210
        b.a211 = 211
        b.a212 = 212
        b.a213 = 213
        b.a214 = 214
        b.a215 = 215
        b.a216 = 216
        b.a217 = 217
        b.a218 = 218
        b.a219 = 219
        b.a220 = 220
        b.a221 = 221
        b.a222 = 222
        b.a223 = 223
        b.a224 = 224
        b.a225 = 225
        b.a226 = 226
        b.a227 = 227
        b.a228 = 228
        b.a229 = 229
        b.a230 = 230
        b.a231 = 231
        b.a232 = 232
        b.a233 = 233
        b.a234 = 234
        b.a235 = 235
        b.a236 = 236
        b.a237 = 237
        b.a238 = 238
        b.a239 = 239
        b.a240 = 240
        b.a241 = 241
        b.a242 = 242
        b.a243 = 243
        b.a244 = 244
        b.a245 = 245
        b.a246 = 246
        b.a247 = 247
        b.a248 = 248
        b.a249 = 249
        b.a250 = 250
        b.a251 = 251
        b.a252 = 252
        b.a253 = 253
        b.a254 = 254
        b.a255 = 255
        b.a256 = 256
        for i in range(10):
            b.a258 = 1
            print(b.a258, dt)
        b.a257 = 257

    def f3(dt):
        try:
            print(4, dt)
            raise RuntimeError
        except:
            print("error runtime")
        finally:
            print("finaly.", dt)

    merge_list = [f1, f2, f3]
    for f in merge_list:
        print(f"===== {f.__name__} =====")
        dis.dis(f)
        print(f"{f.__code__.co_exceptiontable}")
    f_merged = merge_func("f_merge", merge_list)
    dis.dis(f_merged)

    print('\n')
    print("--- call each origin function ---")
    for f in merge_list:
        f("hi")
    print("--- call merged function ---")
    f_merged("hi")
