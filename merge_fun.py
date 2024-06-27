# -*- coding: utf-8 -*-

import sys
import dis
import types
import opcode
import queue

assert sys.version_info.major == 3 and sys.version_info.minor == 11, "For python 3.11 only."

cache_entries = opcode._inline_cache_entries

backward_jrel = (
    opcode.opmap["JUMP_BACKWARD_NO_INTERRUPT"],
    opcode.opmap["JUMP_BACKWARD"],
    opcode.opmap["POP_JUMP_BACKWARD_IF_NOT_NONE"],
    opcode.opmap["POP_JUMP_BACKWARD_IF_NONE"],
    opcode.opmap["POP_JUMP_BACKWARD_IF_FALSE"],
    opcode.opmap["POP_JUMP_BACKWARD_IF_TRUE"],
)

FAST_2_DEREF = {
    opcode.opmap['LOAD_FAST']: opcode.opmap['LOAD_DEREF'],
    opcode.opmap['STORE_FAST']: opcode.opmap['STORE_DEREF'],
    opcode.opmap['DELETE_FAST']: opcode.opmap['DELETE_DEREF'],
}


def _parse_varint(iterator):
    b = next(iterator)
    val = b & 63
    while b & 64:
        val <<= 6
        b = next(iterator)
        val |= b & 63
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
    res.append(val & 63)
    val >>= 6
    while val > 0:
        res.append(64 | val & 63)
        val >>= 6
    bytes += reversed(res)


def write_exception_table(enties):
    res = bytes()
    for entry in enties:
        arr = []
        _write_varint(arr, entry[0] // 2)  # start
        arr[0] |= 0b1000000
        _write_varint(arr, (entry[1] - entry[0]) // 2)  # length
        _write_varint(arr, entry[2] // 2)  # target
        _write_varint(arr, entry[3])  # depth,lasti
        res += bytes(arr)
    return res


def next_entry_index(linetable, start):
    ret = -1
    for i in range(start + 1, len(linetable)):
        if linetable[i] & 0b10000000:
            ret = i
            break
    return ret


def merge_func(func_name, funcs, def_argcount=None, debug=1, merged_firstlineno=0):
    func_info = dict()
    context = {
        "co_names": list(),
        "co_varnames": list(),
        "co_consts": list(),
        "co_freevars": list(),
        "co_cellvars": list(),
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
        "func_globals": dict(),  # __globals__
        "func_defaults": list(),  # __defaults__
        "func_closure": list(),  # __closure__
        "slot_mapping_name": [],
        "name_mapping_slot": {},
    }
    # assert that all functions have the same signature.
    context["co_argcount"] = def_argcount if def_argcount is not None else funcs[0].__code__.co_argcount
    context["co_posonlyargcount"] = funcs[0].__code__.co_posonlyargcount
    context["co_kwonlyargcount"] = funcs[0].__code__.co_kwonlyargcount
    context["co_flags"] = funcs[0].__code__.co_flags

    # merge co_varnames, co_cellvars before convert opcode.
    c_vns = list()
    c_cvs = list()
    for i, func in enumerate(funcs):
        code = func.__code__
        vns = code.co_varnames
        cvs = code.co_cellvars
        for vn in vns:
            if vn not in c_vns and vn in vns and vn in cvs:  # argument names
                c_vns.append(vn)
            elif vn not in c_vns and vn not in cvs and vn not in c_cvs:  # local variable names and not cell.
                c_vns.append(vn)
        for cv in cvs:
            if cv not in c_cvs:
                c_cvs.append(cv)
                if cv not in vns and cv in c_vns:
                    c_vns.remove(cv)

    context['co_varnames'] = c_vns
    context['co_cellvars'] = c_cvs

    names = c_vns + list(set(c_cvs) - set(c_vns))
    context['slot_mapping_name'] = names
    for i, name in enumerate(names):
        context['name_mapping_slot'][name] = i

    merged_code = list()

    # generate MAKE_CELL
    for cv in c_cvs:
        merged_code.append(opcode.opmap['MAKE_CELL'])
        sloti = context['name_mapping_slot'][cv]
        assert 0 <= sloti < 256, "fast slot index need be in [0, 256)"  # TODO: heng, deal with EXTENDED_ARG?
        merged_code.append(sloti)

    # MAKE_CELL location entries, 0b10000000 & 0b01111000
    # @see Objects/locations.md
    context['co_linetable'] += bytes([0b11111000] * len(c_cvs))
    context['co_codelen'] = len(merged_code)

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
        data["co_firstlineno"] = code_obj.co_firstlineno
        data["co_cellvars"] = code_obj.co_cellvars
        data["co_freevars"] = code_obj.co_freevars
        data['co_linetable'] = code_obj.co_linetable
        data["co_exceptiontable"] = parse_exception_table(code_obj)
        data["func_globals"] = func.__globals__
        data["func_defaults"] = func.__defaults__
        data["func_closure"] = func.__closure__

        names = list(code_obj.co_varnames) + list(set(code_obj.co_cellvars) - set(code_obj.co_varnames))
        data['slot_mapping_name'] = names
        data['name_mapping_slot'] = {}
        for i, name in enumerate(names):
            data['name_mapping_slot'][name] = i

        cocode = list(code_obj.co_code)
        linetable = list(code_obj.co_linetable)
        lti = 0  # linetable index
        nei = next_entry_index(linetable, 0)
        is_last = idx == len(funcs) - 1
        cl = data["co_codelen"] = len(cocode)

        # convert opcode and maintain linetable
        tmpcode = []
        inserts = queue.Queue()
        jumps = []  # record jump opcode
        i = 0
        tmplinetable = []
        while i < cl:
            fi = i
            while cocode[fi] == opcode.opmap['EXTENDED_ARG']:
                fi += 2
            op = cocode[fi]

            if op >= opcode.HAVE_ARGUMENT:
                if op in opcode.hasjrel:
                    jumps.append([i, len(tmpcode), fi + 2 - i, 0])

                if op == opcode.opmap['LOAD_CONST'] and cocode[fi + 2] == opcode.opmap['RETURN_VALUE'] and not is_last:
                    codes = make_jump_forward((cl - i) // 2)
                    jumps.append([i, len(tmpcode), len(codes), 0])
                    tmpcode.extend(codes)
                    pbs = linetable[lti:nei]
                    pbs[0] &= 0b11111000
                    pbs[0] |= (len(codes) // 2) - 1
                    tmplinetable.extend(pbs)
                    lti = nei
                    nei = next_entry_index(linetable, lti)

                    if (fi - i + 2) < len(codes):
                        dels = (len(codes) - (fi - i + 2)) // 2
                        for _ in range(dels):
                            tmpcode.extend([opcode.opmap['NOP'], 0])
                            # Number of instructions is increased, append a position entry.
                            tmplinetable.append(0b10000000)
                    else:
                        inserted = (len(codes) - (fi - i + 2)) // 2
                        for _ in range(inserted):
                            inserts.put(i)
                    # Replace RETURN_VALUE by NOP, to keep the number of instructions constant.
                    tmpcode.extend([opcode.opmap['NOP'], 0])
                    fi += 2
                else:
                    try:
                        handler = REGISTER_HANDLES[op]
                    except:
                        raise Exception(f"opcode [{op}]:{opcode.opname[op]} dont have converter.")
                    result, inserted = handler(cocode[i: fi + 2], context, data)
                    tmpcode.extend(result)
                    pbs = linetable[lti:nei]
                    pbs[0] &= 0b11111000
                    pbs[0] |= (len(result) // 2 + cache_entries[op]) - 1
                    tmplinetable.extend(pbs)
                    for _ in range(inserted):
                        inserts.put(i)
                fi += 2
            else:  # opcode with no argument.
                if op == opcode.opmap['RETURN_VALUE'] and not is_last:
                    # Replace with JUMP_FORWARD
                    codes = make_jump_forward((cl - fi) // 2)
                    inserted = (len(codes) - 2) // 2
                    jumps.append([i, len(tmpcode), len(codes), 0])
                    tmpcode.extend(codes)
                    pbs = linetable[lti:nei]
                    pbs[0] &= 0b11111000
                    pbs[0] |= (len(codes) // 2 - 1)
                    tmplinetable.extend(pbs)
                    for _ in range(inserted):
                        inserts.put(i)
                else:
                    tmpcode.extend(cocode[fi: fi + 2])
                    tmplinetable.extend(linetable[lti:nei])
                fi += 2

            lti = nei
            nei = next_entry_index(linetable, lti)
            # CACHE
            if cache_entries[op] > 0:
                tmpcode.extend(cocode[fi: fi + cache_entries[op] * 2])
                fi += cache_entries[op] * 2

            i = fi

        # after converted, deal with relative jump opcode.
        allinserted = []
        while not inserts.empty():
            iat = inserts.get()
            allinserted.append(iat)
            for jump in jumps:
                jcodes = cocode[jump[0]: jump[0] + jump[2]]
                jarg = 0
                for i in range(-1, -len(jcodes), -2):
                    jarg |= jcodes[i] << (abs(i) // 2 * 8)
                jop = jcodes[-2]
                cover = True
                if jop not in backward_jrel:
                    cover = jump[0] < iat < jump[0] + jump[2] + jarg * 2
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
                jop = cocode[jump[0]: jump[0] + jump[2]][-2]
                while jarg > 0:
                    word = jarg & 0xFF
                    jarg = jarg >> 8
                    if cvt:
                        cvt = [opcode.EXTENDED_ARG, word] + cvt
                    else:
                        cvt = [jop, word]
                tmpcode = tmpcode[0: jump[1]] + cvt + tmpcode[jump[1] + jump[2]:]

        # deal with exception table.
        if data['co_exceptiontable']:
            exc_deltas = [0, 0, 0] * len(data['co_exceptiontable'])
            for i in range(len(data['co_exceptiontable'])):
                entry = data['co_exceptiontable'][i]
                start = entry[0]
                end = entry[1] - 2
                target = entry[2]
                for isa in allinserted:
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
        merged_code += tmpcode
        context["co_names"].extend(data["co_names"])
        if data["co_renames"]:
            context["co_names"].extend(e[1] for e in data["co_renames"])  # extend new names
        context["co_consts"].extend(data["co_consts"])
        context["co_freevars"].extend(data["co_freevars"])
        context["co_stacksize"] = max(data["co_stacksize"], context["co_stacksize"])
        context["co_exceptiontable"] += write_exception_table(data["co_exceptiontable"])
        context["co_codelen"] = len(merged_code)
        context['co_linetable'] += bytes(tmplinetable)

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
    context['co_nlocals'] = len(context['co_varnames'])
    for k, v in context.items():
        if type(v) is list:
            context[k] = tuple(v)

    mycode_obj = types.CodeType(
        context["co_argcount"],  # number of arguments (not including * or ** args)
        context["co_posonlyargcount"],  # int, 函数的仅限位置 形参 的总数（包括具有默认值的参数）
        context["co_kwonlyargcount"],  # int, 函数的仅限关键字 形参 的数量（包括具有默认值的参数
        context["co_nlocals"],  # number of local varialbes (except cell)
        context["co_stacksize"] + 10,  # int, 取max_stacksize+1, +1 是为了避免内存crash
        context["co_flags"],  # bitmap: 1=optimized | 2=newlocals | 4=*arg | 8=**arg
        context["co_code"],  # bytes of raw compiled bytecode
        context["co_consts"],  # tuple of constants used in the bytecode
        context["co_names"],  # tuple of names of local variables
        context["co_varnames"],  # tuple of names of arguments and local variables
        f"merge_funcion_generated__{func_name}.py",  # filename
        func_name,  # str, function name
        func_name,  # qualname
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
    arg = 0
    op = opbytes[-2]
    for i in range(-1, -len(opbytes), -2):
        arg |= opbytes[i] << (abs(i) // 2 * 8)

    offset = len(context.get("co_names"))
    arg += offset

    res = []
    if arg == 0:
        res = [op, 0]
    while arg > 0:
        word = arg & 0xFF
        arg = arg >> 8
        if res:
            res = [opcode.EXTENDED_ARG, word] + res
        else:
            res = [op, word]
    return res, (len(res) - len(opbytes)) // 2


def convert_co_renames(opbytes, context, data):
    """由于合并了func_globals, co_names, 全局变量读取的位置变更，重命名"""
    arg = 0
    op = opbytes[-2]
    for i in range(-1, -len(opbytes), -2):
        arg |= opbytes[i] << (abs(i) // 2 * 8)

    offset = len(context.get("co_names"))
    pushnull = arg & 0x01
    if op == opcode.opmap["LOAD_GLOBAL"]:
        namei = arg >> 1
    else:
        namei = arg

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

    if op == opcode.opmap["LOAD_GLOBAL"]:
        arg = (current << 1) | (0x01 if pushnull else 0x00)
    else:
        arg = current

    res = []
    if arg == 0:
        res = [op, 0]
    while arg > 0:
        word = arg & 0xFF
        arg = arg >> 8
        if res:
            res = [opcode.EXTENDED_ARG, word] + res
        else:
            res = [op, word]

    return res, (len(res) - len(opbytes)) // 2


def convert_co_consts(opbytes, context, data):
    """由于合并了co_consts,常量读取的位置变更"""
    arg = 0
    op = opbytes[-2]
    for i in range(-1, -len(opbytes), -2):
        arg |= opbytes[i] << (abs(i) // 2 * 8)

    offset = len(context.get("co_consts"))
    arg += offset

    res = []
    if arg == 0:
        res = [op, 0]
    while arg > 0:
        word = arg & 0xFF
        arg = arg >> 8
        if res:
            res = [opcode.EXTENDED_ARG, word] + res
        else:
            res = [op, word]
    return res, (len(res) - len(opbytes)) // 2


def convert_varnames(opbytes, context, data):
    """由于合并了co_varnames,局部变量读取的位置变更"""
    arg = 0
    op = opbytes[-2]
    for i in range(-1, -len(opbytes), -2):
        arg |= opbytes[i] << (abs(i) // 2 * 8)

    name = data['co_varnames'][arg]
    if name in context['co_cellvars']:
        op = FAST_2_DEREF[op]
        arg = context['name_mapping_slot'][name]
    else:
        arg = context.get("co_varnames").index(name)

    res = []
    if arg == 0:
        res = [op, 0]
    while arg > 0:
        word = arg & 0xFF
        arg = arg >> 8
        if res:
            res = [opcode.EXTENDED_ARG, word] + res
        else:
            res = [op, word]
    return res, (len(res) - len(opbytes)) // 2


def convert_closure(opbytes, context, data):
    """由于合并了 co_cellvars，cell index 需要变更"""
    arg = 0
    op = opbytes[-2]
    for i in range(-1, -len(opbytes), -2):
        arg |= opbytes[i] << (abs(i) // 2 * 8)

    name = data['slot_mapping_name'][arg]
    arg = context['name_mapping_slot'][name]

    res = []
    if arg == 0:
        res = [op, 0]
    while arg > 0:
        word = arg & 0xFF
        arg = arg >> 8
        if res:
            res = [opcode.EXTENDED_ARG, word] + res
        else:
            res = [op, word]

    return res, (len(res) - len(opbytes)) // 2


def convert_default(opbytes, context, data):
    """参数不做任何变化"""
    return opbytes, 0


def convert_nop(opbytes, context, data):
    """全部替换为NOP"""
    for i in range(0, len(opbytes), 2):
        opbytes[i] = opcode.opmap['NOP']
        opbytes[i + 1] = 0
    return opbytes, 0


def make_jump_forward(delta):
    op = opcode.opmap["JUMP_FORWARD"]
    arg = delta
    res = []
    if arg == 0:
        res = [op, 0]
    while arg > 0:
        word = arg & 0xFF
        arg >>= 8
        if res:
            res = [opcode.EXTENDED_ARG, word] + res
        else:
            res = [op, word]
    return res


REGISTER_HANDLES = {
    opcode.opmap["MAKE_CELL"]: convert_nop,  # MAKE_CELL 提前到最前，中间的使用NOP填，方面处理 hasjrel 指令
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
    print(f"co_linetable: {co.co_linetable}")
    print(f"co_exceptiontable: {co.co_exceptiontable}")
    print(f"co_freevars: {co.co_freevars}")
    print(f"co_cellvars: {co.co_cellvars}")

    print("== code ==")
    dis.dis(f)

