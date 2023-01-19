import ctypes
import numbers
import math
import mlir
import numpy as np
from typing import Union, Optional
from mlir import ir
from mlir import passmanager
from mlir import execution_engine
from mlir import runtime
from mlir import dialects

from mlir.dialects import arith
from mlir.dialects import bufferization
from mlir.dialects import func
from mlir.dialects import linalg
from mlir.dialects import sparse_tensor
from mlir.dialects import tensor
from mlir.dialects import scf
from mlir.dialects import memref
from mlir.dialects.sparse_tensor import DimLevelType

from .tensor import SparseTensorBase, SparseTensor, Matrix, Vector, Scalar, TransposedMatrix
from .operators import UnaryOp, BinaryOp, SelectOp, IndexUnaryOp, Monoid, Semiring
from .compiler import compile, engine_cache
from . descriptor import Descriptor, NULL as NULL_DESC
from .utils import get_sparse_output_pointer, get_scalar_output_pointer
from .types import RankedTensorType, BOOL, INT64, FP64


# TODO: vec->matrix broadcasting as builtin param in apply_mask (rowwise/colwise)
def apply_mask(sp: SparseTensorBase, mask: SparseTensor, desc: Descriptor = NULL_DESC):
    """
    The only elements which survive in `sp` are those with corresponding elements in
    `mask` which are "truthy" (i.e. non-zero).
    If desc.mask_structure is True, then all elements found in the mask are treated as
    being "truthy" (e.g. only the structure is considered, not the actual values).

    If desc.mask_complement is True, the inverse logic is applied and the surviving elements
    in `sp` correspond to missing or "falsy" elements in the mask.
    """
    assert mask.ndims == sp.ndims
    assert mask._sparsity == sp._sparsity
    if mask.shape != sp.shape:
        raise GrbDimensionMismatch(f"Mask shape mismatch: {mask.shape} != {sp.shape}")

    rank = sp.ndims
    if rank == 0:  # Scalar
        s = Scalar.new(sp.dtype)
        if mask._obj is not None:
            s.set_element(sp.extract_element())
        return s

    if not isinstance(mask, SparseTensor):
        raise TypeError(f"Mask must be Vector or Matrix, not {type(mask)}")

    # Convert value mask to structural mask
    if not desc.mask_structure:
        zero = Scalar.new(mask.dtype)
        zero.set_element(0)
        mask = select(SelectOp.valuene, mask, thunk=zero)

    # Build and compile if needed
    key = ('apply_mask', *sp.get_loop_key(), *mask.get_loop_key(), desc.mask_complement)
    if key not in engine_cache:
        engine_cache[key] = _build_apply_mask(mask, sp, desc.mask_complement)

    # Call the compiled function
    mem_out = get_sparse_output_pointer()
    arg_pointers = [mask._obj, sp._obj, mem_out]
    engine_cache[key].invoke('main', *arg_pointers)
    return mask.baseclass(sp.dtype, mask.shape, mem_out, mask._sparsity,
                          mask.perceived_ordering, intermediate_result=True)


def _build_apply_mask(mask: SparseTensor, sp: SparseTensorBase, complement: bool):
    with ir.Context(), ir.Location.unknown():
        module = ir.Module.create()
        with ir.InsertionPoint(module.body):
            rank = sp.ndims
            index = ir.IndexType.get()
            dtype_sp = sp.dtype.build_mlir_type()
            dtype_mask = mask.dtype.build_mlir_type()
            dtype_out = dtype_sp
            perm_sp = ir.AffineMap.get_permutation(sp.permutation)
            perm_mask = ir.AffineMap.get_permutation(mask.permutation)
            perm_out = ir.AffineMap.get_permutation(range(rank))
            rtt_sp = sp.rtt.as_mlir_type()
            rtt_mask = mask.rtt.as_mlir_type()
            rtt_out = mask.rtt.copy(dtype=sp.dtype).as_mlir_type()

            @func.FuncOp.from_py_func(rtt_mask, rtt_sp)
            def main(msk, x):
                c = [arith.ConstantOp(index, i) for i in range(rank)]
                dims = [tensor.DimOp(x, c[i]).result for i in mask.permutation]
                out = bufferization.AllocTensorOp(rtt_out, dims, None, None, False)
                generic_op = linalg.GenericOp(
                    [rtt_out],
                    [msk, x],
                    [out],
                    ir.ArrayAttr.get([ir.AffineMapAttr.get(p) for p in (perm_mask, perm_sp, perm_out)]),
                    ir.ArrayAttr.get([ir.Attribute.parse('#linalg.iterator_type<parallel>')]*rank)
                )
                block = generic_op.regions[0].blocks.append(dtype_mask, dtype_sp, dtype_out)
                with ir.InsertionPoint(block):
                    m, a, o = block.arguments
                    res = sparse_tensor.BinaryOp(dtype_out, m, a, right_identity=complement)
                    if not complement:
                        overlap = res.regions[0].blocks.append(dtype_mask, dtype_sp)
                        with ir.InsertionPoint(overlap):
                            _, arg0 = overlap.arguments
                            sparse_tensor.YieldOp(result=arg0)
                    linalg.YieldOp([res])
                return generic_op.result
            main.func_op.attributes["llvm.emit_c_interface"] = ir.UnitAttr.get()

        return compile(module)


def nvals(sp: SparseTensorBase):
    # Build and compile if needed
    key = ('nvals', *sp.get_loop_key())
    if key not in engine_cache:
        engine_cache[key] = _build_nvals(sp)

    # Call the compiled function
    mem_out = ctypes.pointer(ctypes.c_long(0))
    arg_pointers = [sp._obj, mem_out]
    engine_cache[key].invoke('main', *arg_pointers)
    return mem_out.contents.value


def _build_nvals(sp: SparseTensorBase):
    with ir.Context(), ir.Location.unknown():
        module = ir.Module.create()
        with ir.InsertionPoint(module.body):
            rtt = sp.rtt.as_mlir_type()

            @func.FuncOp.from_py_func(rtt)
            def main(x):
                nvals = sparse_tensor.NumberOfEntriesOp(x)
                return arith.IndexCastOp(INT64.build_mlir_type(), nvals)

            main.func_op.attributes["llvm.emit_c_interface"] = ir.UnitAttr.get()
        return compile(module)


def dup(sp: SparseTensorBase, intermediate: bool = True):
    if sp._obj is None:
        return sp.baseclass(sp.dtype, sp.shape, intermediate_result=intermediate)

    # Build and compile if needed
    key = ('dup', *sp.get_loop_key())
    if key not in engine_cache:
        engine_cache[key] = _build_dup(sp)

    # Call the compiled function
    mem_out = get_sparse_output_pointer()
    arg_pointers = [sp._obj, mem_out]
    engine_cache[key].invoke('main', *arg_pointers)
    return sp.baseclass(sp.dtype, sp.shape, mem_out, sp._sparsity,
                        sp.perceived_ordering, intermediate_result=intermediate)


def _build_dup(sp: SparseTensorBase):
    with ir.Context(), ir.Location.unknown():
        module = ir.Module.create()
        with ir.InsertionPoint(module.body):
            rank = sp.ndims
            index = ir.IndexType.get()
            dtype = sp.dtype.build_mlir_type()
            perm = ir.AffineMap.get_permutation(sp.permutation)
            perm_out = ir.AffineMap.get_permutation(range(rank))
            rtt = sp.rtt.as_mlir_type()
            rtt_out = sp.rtt.copy(ordering=sp.perceived_ordering).as_mlir_type()

            @func.FuncOp.from_py_func(rtt)
            def main(x):
                c = [arith.ConstantOp(index, i) for i in range(rank)]
                dims = [tensor.DimOp(x, c[i]).result for i in sp.permutation]
                out = bufferization.AllocTensorOp(rtt_out, dims, None, None, False)
                generic_op = linalg.GenericOp(
                    [rtt_out],
                    [x],
                    [out],
                    ir.ArrayAttr.get([ir.AffineMapAttr.get(p) for p in (perm, perm_out)]),
                    ir.ArrayAttr.get([ir.Attribute.parse('#linalg.iterator_type<parallel>')]*rank)
                )
                block = generic_op.regions[0].blocks.append(dtype, dtype)
                with ir.InsertionPoint(block):
                    a, _ = block.arguments
                    linalg.YieldOp([a])
                return generic_op.result
            main.func_op.attributes["llvm.emit_c_interface"] = ir.UnitAttr.get()

        return compile(module)


def ewise_add(op: BinaryOp, left: SparseTensorBase, right: SparseTensorBase):
    assert left.ndims == right.ndims
    assert left.dtype == right.dtype
    assert left._sparsity == right._sparsity

    rank = left.ndims
    if rank == 0:  # Scalar
        # TODO: implement this
        raise NotImplementedError("doesn't yet work for Scalar")

    # TODO: handle case of either left or right not having an _obj -> result will be other for ewise_add
    #       or have a utility to build an empty MLIRSparseTensor for all input tensors?

    # Build and compile if needed
    key = ('ewise_add', op.name, *left.get_loop_key(), *right.get_loop_key())
    if key not in engine_cache:
        engine_cache[key] = _build_ewise_add(op, left, right)

    # Call the compiled function
    mem_out = get_sparse_output_pointer()
    arg_pointers = [left._obj, right._obj, mem_out]
    engine_cache[key].invoke('main', *arg_pointers)
    return left.baseclass(op.get_output_type(left.dtype), left.shape, mem_out,
                          left._sparsity, left.perceived_ordering, intermediate_result=True)


def _build_ewise_add(op: BinaryOp, left: SparseTensorBase, right: SparseTensorBase):
    with ir.Context(), ir.Location.unknown():
        module = ir.Module.create()
        with ir.InsertionPoint(module.body):
            rank = left.ndims
            index = ir.IndexType.get()
            dtype = left.dtype.build_mlir_type()
            perm_left = ir.AffineMap.get_permutation(left.permutation)
            perm_right = ir.AffineMap.get_permutation(right.permutation)
            perm_out = ir.AffineMap.get_permutation(range(rank))
            rtt_left = left.rtt.as_mlir_type()
            rtt_right = right.rtt.as_mlir_type()
            rtt_out = left.rtt.copy(ordering=left.perceived_ordering).as_mlir_type()

            @func.FuncOp.from_py_func(rtt_left, rtt_right)
            def main(x, y):
                c = [arith.ConstantOp(index, i) for i in range(rank)]
                dims = [tensor.DimOp(x, c[i]).result for i in left.permutation]
                out = bufferization.AllocTensorOp(rtt_out, dims, None, None, False)
                generic_op = linalg.GenericOp(
                    [rtt_out],
                    [x, y],
                    [out],
                    ir.ArrayAttr.get([ir.AffineMapAttr.get(p) for p in (perm_left, perm_right, perm_out)]),
                    ir.ArrayAttr.get([ir.Attribute.parse('#linalg.iterator_type<parallel>')]*rank)
                )
                block = generic_op.regions[0].blocks.append(dtype, dtype, dtype)
                with ir.InsertionPoint(block):
                    a, b, o = block.arguments
                    res = sparse_tensor.BinaryOp(dtype, a, b, left_identity=True, right_identity=True)
                    overlap = res.regions[0].blocks.append(dtype, dtype)
                    with ir.InsertionPoint(overlap):
                        arg0, arg1 = overlap.arguments
                        overlap_res = op(arg0, arg1)
                        sparse_tensor.YieldOp(result=overlap_res)
                    linalg.YieldOp([res])
                return generic_op.result
            main.func_op.attributes["llvm.emit_c_interface"] = ir.UnitAttr.get()

        return compile(module)


def ewise_mult(op: BinaryOp, left: SparseTensorBase, right: SparseTensorBase):
    assert left.ndims == right.ndims
    assert left.dtype == right.dtype
    assert left._sparsity == right._sparsity

    rank = left.ndims
    if rank == 0:  # Scalar
        # TODO: implement this
        raise NotImplementedError("doesn't yet work for Scalar")

    # TODO: handle case of either left or right not having an _obj -> result will be empty for ewise_mult

    # Build and compile if needed
    key = ('ewise_mult', op.name, *left.get_loop_key(), *right.get_loop_key())
    if key not in engine_cache:
        engine_cache[key] = _build_ewise_mult(op, left, right)

    # Call the compiled function
    mem_out = get_sparse_output_pointer()
    arg_pointers = [left._obj, right._obj, mem_out]
    engine_cache[key].invoke('main', *arg_pointers)
    return left.baseclass(op.get_output_type(left.dtype), left.shape, mem_out,
                          left._sparsity, left.perceived_ordering, intermediate_result=True)


def _build_ewise_mult(op: BinaryOp, left: SparseTensorBase, right: SparseTensorBase):
    op_result_dtype = op.get_output_type(left.dtype)
    with ir.Context(), ir.Location.unknown():
        module = ir.Module.create()
        with ir.InsertionPoint(module.body):
            rank = left.ndims
            index = ir.IndexType.get()
            dtype = left.dtype.build_mlir_type()
            dtype_out = op_result_dtype.build_mlir_type()
            perm_left = ir.AffineMap.get_permutation(left.permutation)
            perm_right = ir.AffineMap.get_permutation(right.permutation)
            perm_out = ir.AffineMap.get_permutation(range(rank))
            rtt_left = left.rtt.as_mlir_type()
            rtt_right = right.rtt.as_mlir_type()
            rtt_out = left.rtt.copy(dtype=op_result_dtype, ordering=left.perceived_ordering).as_mlir_type()

            @func.FuncOp.from_py_func(rtt_left, rtt_right)
            def main(x, y):
                c = [arith.ConstantOp(index, i) for i in range(rank)]
                dims = [tensor.DimOp(x, c[i]).result for i in left.permutation]
                out = bufferization.AllocTensorOp(rtt_out, dims, None, None, False)
                generic_op = linalg.GenericOp(
                    [rtt_out],
                    [x, y],
                    [out],
                    ir.ArrayAttr.get([ir.AffineMapAttr.get(p) for p in (perm_left, perm_right, perm_out)]),
                    ir.ArrayAttr.get([ir.Attribute.parse('#linalg.iterator_type<parallel>')]*rank)
                )
                block = generic_op.regions[0].blocks.append(dtype, dtype, dtype_out)
                with ir.InsertionPoint(block):
                    a, b, o = block.arguments
                    res = sparse_tensor.BinaryOp(dtype_out, a, b)
                    overlap = res.regions[0].blocks.append(dtype, dtype)
                    with ir.InsertionPoint(overlap):
                        arg0, arg1 = overlap.arguments
                        overlap_res = op(arg0, arg1)
                        sparse_tensor.YieldOp(result=overlap_res)
                    linalg.YieldOp([res])
                return generic_op.result
            main.func_op.attributes["llvm.emit_c_interface"] = ir.UnitAttr.get()

        return compile(module)


# TODO: pass the mask to mxm
def mxm(op: Semiring, left: Union[Matrix, TransposedMatrix], right: Union[Matrix, TransposedMatrix]):
    assert left.ndims == right.ndims == 2
    assert left.dtype == right.dtype
    assert left._sparsity == right._sparsity

    # TODO: handle case of either left or right not having an _obj -> result will be empty for mxm

    # Build and compile if needed
    key = ('mxm', op.name, *left.get_loop_key(), *right.get_loop_key())
    if key not in engine_cache:
        engine_cache[key] = _build_mxm(op, left, right)

    # Call the compiled function
    mem_out = get_sparse_output_pointer()
    arg_pointers = [left._obj, right._obj, mem_out]
    engine_cache[key].invoke('main', *arg_pointers)
    return Matrix(op.binop.get_output_type(left.dtype), [left.shape[0], right.shape[1]], mem_out,
                  left._sparsity, left.perceived_ordering, intermediate_result=True)


def _build_mxm(op: Semiring, left: Union[Matrix, TransposedMatrix], right: Union[Matrix, TransposedMatrix]):
    op_result_dtype = op.binop.get_output_type(left.dtype)
    with ir.Context(), ir.Location.unknown():
        module = ir.Module.create()
        with ir.InsertionPoint(module.body):
            index = ir.IndexType.get()
            dtype = left.dtype.build_mlir_type()
            dtype_out = op_result_dtype.build_mlir_type()
            perm_left = ir.AffineMap.get(3, 0, left._permute([ir.AffineDimExpr.get(0), ir.AffineDimExpr.get(2)]))
            perm_right = ir.AffineMap.get(3, 0, right._permute([ir.AffineDimExpr.get(2), ir.AffineDimExpr.get(1)]))
            perm_out = ir.AffineMap.get(3, 0, [ir.AffineDimExpr.get(0), ir.AffineDimExpr.get(1)])
            rtt_left = left.rtt.as_mlir_type()
            rtt_right = right.rtt.as_mlir_type()
            rtt_out = left.rtt.copy(dtype=op_result_dtype, ordering=left.perceived_ordering).as_mlir_type()

            @func.FuncOp.from_py_func(rtt_left, rtt_right)
            def main(x, y):
                c = [arith.ConstantOp(index, i) for i in range(2)]
                nrows = tensor.DimOp(x, c[left.permutation[0]]).result
                ncols = tensor.DimOp(y, c[right.permutation[1]]).result
                out = bufferization.AllocTensorOp(rtt_out, [nrows, ncols], None, None, False)
                generic_op = linalg.GenericOp(
                    [rtt_out],
                    [x, y],
                    [out],
                    ir.ArrayAttr.get([ir.AffineMapAttr.get(p) for p in (perm_left, perm_right, perm_out)]),
                    ir.ArrayAttr.get([
                        ir.Attribute.parse('#linalg.iterator_type<parallel>'),
                        ir.Attribute.parse('#linalg.iterator_type<parallel>'),
                        ir.Attribute.parse('#linalg.iterator_type<reduction>'),
                    ])
                )
                block = generic_op.regions[0].blocks.append(dtype, dtype, dtype_out)
                with ir.InsertionPoint(block):
                    a, b, o = block.arguments
                    bin_result = sparse_tensor.BinaryOp(dtype_out, a, b)
                    overlap = bin_result.regions[0].blocks.append(dtype, dtype)
                    with ir.InsertionPoint(overlap):
                        arg0, arg1 = overlap.arguments
                        overlap_res = op.binop(arg0, arg1)
                        sparse_tensor.YieldOp(result=overlap_res)
                    ident = op.monoid.identity(op_result_dtype)
                    red_result = sparse_tensor.ReduceOp(bin_result, o, ident)
                    reduce = red_result.regions[0].blocks.append(dtype_out, dtype_out)
                    with ir.InsertionPoint(reduce):
                        arg0, arg1 = reduce.arguments
                        reduce_res = op.monoid.binop(arg0, arg1)
                        sparse_tensor.YieldOp(result=reduce_res)
                    linalg.YieldOp([red_result])
                return generic_op.result
            main.func_op.attributes["llvm.emit_c_interface"] = ir.UnitAttr.get()

        return compile(module)


# TODO: pass the mask to mxv
def mxv(op: Semiring, left: Union[Matrix, TransposedMatrix], right: Vector):
    assert left.ndims == 2
    assert right.ndims == 1
    assert left.dtype == right.dtype

    # TODO: handle case of either left or right not having an _obj -> result will be empty for mxv

    # Build and compile if needed
    key = ('mxv', op.name, *left.get_loop_key(), *right.get_loop_key())
    if key not in engine_cache:
        engine_cache[key] = _build_mxv(op, left, right)

    # Call the compiled function
    mem_out = get_sparse_output_pointer()
    arg_pointers = [left._obj, right._obj, mem_out]
    engine_cache[key].invoke('main', *arg_pointers)
    return Vector(op.binop.get_output_type(left.dtype), [left.shape[0]], mem_out,
                  right._sparsity, right.perceived_ordering, intermediate_result=True)


def _build_mxv(op: Semiring, left: Union[Matrix, TransposedMatrix], right: Vector):
    op_result_dtype = op.binop.get_output_type(left.dtype)
    with ir.Context(), ir.Location.unknown():
        module = ir.Module.create()
        with ir.InsertionPoint(module.body):
            index = ir.IndexType.get()
            dtype = left.dtype.build_mlir_type()
            dtype_out = op_result_dtype.build_mlir_type()
            perm_left = ir.AffineMap.get(2, 0, left._permute([ir.AffineDimExpr.get(0), ir.AffineDimExpr.get(1)]))
            perm_right = ir.AffineMap.get(2, 0, right._permute([ir.AffineDimExpr.get(1)]))
            perm_out = ir.AffineMap.get(2, 0, [ir.AffineDimExpr.get(0)])
            rtt_left = left.rtt.as_mlir_type()
            rtt_right = right.rtt.as_mlir_type()
            rtt_out = right.rtt.copy(dtype=op_result_dtype).as_mlir_type()

            @func.FuncOp.from_py_func(rtt_left, rtt_right)
            def main(x, y):
                c = [arith.ConstantOp(index, i) for i in range(2)]
                size = tensor.DimOp(x, c[left.permutation[0]]).result
                out = bufferization.AllocTensorOp(rtt_out, [size], None, None, False)
                generic_op = linalg.GenericOp(
                    [rtt_out],
                    [x, y],
                    [out],
                    ir.ArrayAttr.get([ir.AffineMapAttr.get(p) for p in (perm_left, perm_right, perm_out)]),
                    ir.ArrayAttr.get([
                        ir.Attribute.parse('#linalg.iterator_type<parallel>'),
                        ir.Attribute.parse('#linalg.iterator_type<reduction>'),
                    ])
                )
                block = generic_op.regions[0].blocks.append(dtype, dtype, dtype_out)
                with ir.InsertionPoint(block):
                    a, b, o = block.arguments
                    bin_result = sparse_tensor.BinaryOp(dtype_out, a, b)
                    overlap = bin_result.regions[0].blocks.append(dtype, dtype)
                    with ir.InsertionPoint(overlap):
                        arg0, arg1 = overlap.arguments
                        overlap_res = op.binop(arg0, arg1)
                        sparse_tensor.YieldOp(result=overlap_res)
                    ident = op.monoid.identity(op_result_dtype)
                    red_result = sparse_tensor.ReduceOp(bin_result, o, ident)
                    reduce = red_result.regions[0].blocks.append(dtype_out, dtype_out)
                    with ir.InsertionPoint(reduce):
                        arg0, arg1 = reduce.arguments
                        reduce_res = op.monoid.binop(arg0, arg1)
                        sparse_tensor.YieldOp(result=reduce_res)
                    linalg.YieldOp([red_result])
                return generic_op.result
            main.func_op.attributes["llvm.emit_c_interface"] = ir.UnitAttr.get()

        return compile(module)


# TODO: pass the mask to vxm
def vxm(op: Semiring, left: Vector, right: Union[Matrix, TransposedMatrix]):
    assert left.ndims == 1
    assert right.ndims == 2
    assert left.dtype == right.dtype

    # TODO: handle case of either left or right not having an _obj -> result will be empty for vxm

    # Build and compile if needed
    key = ('vxm', op.name, *left.get_loop_key(), *right.get_loop_key())
    if key not in engine_cache:
        engine_cache[key] = _build_vxm(op, left, right)

    # Call the compiled function
    mem_out = get_sparse_output_pointer()
    arg_pointers = [left._obj, right._obj, mem_out]
    engine_cache[key].invoke('main', *arg_pointers)
    return Vector(op.binop.get_output_type(left.dtype), [right.shape[1]], mem_out,
                  left._sparsity, left.perceived_ordering, intermediate_result=True)


def _build_vxm(op: Semiring, left: Vector, right: Union[Matrix, TransposedMatrix]):
    op_result_dtype = op.binop.get_output_type(left.dtype)
    with ir.Context(), ir.Location.unknown():
        module = ir.Module.create()
        with ir.InsertionPoint(module.body):
            index = ir.IndexType.get()
            dtype = left.dtype.build_mlir_type()
            dtype_out = op_result_dtype.build_mlir_type()
            perm_left = ir.AffineMap.get(2, 0, left._permute([ir.AffineDimExpr.get(0)]))
            perm_right = ir.AffineMap.get(2, 0, right._permute([ir.AffineDimExpr.get(0), ir.AffineDimExpr.get(1)]))
            perm_out = ir.AffineMap.get(2, 0, [ir.AffineDimExpr.get(1)])
            rtt_left = left.rtt.as_mlir_type()
            rtt_right = right.rtt.as_mlir_type()
            rtt_out = left.rtt.copy(dtype=op_result_dtype).as_mlir_type()

            @func.FuncOp.from_py_func(rtt_left, rtt_right)
            def main(x, y):
                c = [arith.ConstantOp(index, i) for i in range(2)]
                size = tensor.DimOp(y, c[right.permutation[1]]).result
                out = bufferization.AllocTensorOp(rtt_out, [size], None, None, False)
                generic_op = linalg.GenericOp(
                    [rtt_out],
                    [x, y],
                    [out],
                    ir.ArrayAttr.get([ir.AffineMapAttr.get(p) for p in (perm_left, perm_right, perm_out)]),
                    ir.ArrayAttr.get([
                        ir.Attribute.parse('#linalg.iterator_type<reduction>'),
                        ir.Attribute.parse('#linalg.iterator_type<parallel>'),
                    ])
                )
                block = generic_op.regions[0].blocks.append(dtype, dtype, dtype_out)
                with ir.InsertionPoint(block):
                    a, b, o = block.arguments
                    bin_result = sparse_tensor.BinaryOp(dtype_out, a, b)
                    overlap = bin_result.regions[0].blocks.append(dtype, dtype)
                    with ir.InsertionPoint(overlap):
                        arg0, arg1 = overlap.arguments
                        overlap_res = op.binop(arg0, arg1)
                        sparse_tensor.YieldOp(result=overlap_res)
                    ident = op.monoid.identity(op_result_dtype)
                    red_result = sparse_tensor.ReduceOp(bin_result, o, ident)
                    reduce = red_result.regions[0].blocks.append(dtype_out, dtype_out)
                    with ir.InsertionPoint(reduce):
                        arg0, arg1 = reduce.arguments
                        reduce_res = op.monoid.binop(arg0, arg1)
                        sparse_tensor.YieldOp(result=reduce_res)
                    linalg.YieldOp([red_result])
                return generic_op.result
            main.func_op.attributes["llvm.emit_c_interface"] = ir.UnitAttr.get()

        return compile(module)


def apply(op: Union[UnaryOp, BinaryOp, IndexUnaryOp],
          sp: SparseTensorBase,
          left: Optional[Scalar],
          right: Optional[Scalar],
          thunk: Optional[Scalar],
          inplace: bool = False):
    rank = sp.ndims
    if rank == 0:  # Scalar
        # TODO: implement this
        raise NotImplementedError("doesn't yet work for Scalar")

    # Handle case of empty tensor
    if sp._obj is None:
        return sp.__class__(op.get_output_type(sp.dtype), sp.shape)

    # Build and compile if needed
    # Note that Scalars are included in the key because they are inlined in the compiled code
    optype = type(op)
    if optype is UnaryOp:
        key = ('apply_unary', op.name, *sp.get_loop_key(), inplace)
    elif optype is BinaryOp:
        if left is not None:
            key = ('apply_bind_first', op.name, *sp.get_loop_key(), left._obj, inplace)
        else:
            key = ('apply_bind_second', op.name, *sp.get_loop_key(), right._obj, inplace)
    else:
        if inplace:
            raise TypeError("apply inplace not supported for IndexUnaryOp")
        key = ('apply_indexunary', op.name, *sp.get_loop_key(), thunk._obj)
    if key not in engine_cache:
        if inplace:
            engine_cache[key] = _build_apply_inplace(op, sp, left, right)
        else:
            engine_cache[key] = _build_apply(op, sp, left, right, thunk)

    # Call the compiled function
    if inplace:
        engine_cache[key].invoke('main', sp._obj)
        return sp
    mem_out = get_sparse_output_pointer()
    arg_pointers = [sp._obj, mem_out]
    engine_cache[key].invoke('main', *arg_pointers)
    return sp.baseclass(op.get_output_type(sp.dtype), sp.shape, mem_out,
                            sp._sparsity, sp.perceived_ordering, intermediate_result=True)


def _build_apply(op: Union[UnaryOp, BinaryOp, IndexUnaryOp],
                 sp: SparseTensorBase,
                 left: Optional[Scalar],
                 right: Optional[Scalar],
                 thunk: Optional[Scalar]):
    optype = type(op)
    op_result_dtype = op.get_output_type(sp.dtype)
    with ir.Context(), ir.Location.unknown():
        module = ir.Module.create()
        with ir.InsertionPoint(module.body):
            rank = sp.ndims
            index = ir.IndexType.get()
            i64 = ir.IntegerType.get_signless(64)
            dtype = sp.dtype.build_mlir_type()
            dtype_out = op_result_dtype.build_mlir_type()
            perm = ir.AffineMap.get_permutation(sp.permutation)
            perm_out = ir.AffineMap.get_permutation(range(rank))
            rtt = sp.rtt.as_mlir_type()
            rtt_out = sp.rtt.copy(dtype=op_result_dtype, ordering=sp.perceived_ordering).as_mlir_type()

            @func.FuncOp.from_py_func(rtt)
            def main(x):
                c = [arith.ConstantOp(index, i) for i in range(rank)]
                dims = [tensor.DimOp(x, c[i]).result for i in sp.permutation]
                out = bufferization.AllocTensorOp(rtt_out, dims, None, None, False)
                generic_op = linalg.GenericOp(
                    [rtt_out],
                    [x],
                    [out],
                    ir.ArrayAttr.get([ir.AffineMapAttr.get(p) for p in (perm, perm_out)]),
                    ir.ArrayAttr.get([ir.Attribute.parse('#linalg.iterator_type<parallel>')]*rank)
                )
                block = generic_op.regions[0].blocks.append(dtype, dtype_out)
                with ir.InsertionPoint(block):
                    a, o = block.arguments
                    if optype is IndexUnaryOp:
                        rowidx = linalg.IndexOp(ir.IntegerAttr.get(i64, 0))
                        if rank == 2:
                            colidx = linalg.IndexOp(ir.IntegerAttr.get(i64, 1))
                        else:
                            colidx = arith.ConstantOp(index, 0)
                    res = sparse_tensor.UnaryOp(dtype_out, a)
                    present = res.regions[0].blocks.append(dtype)
                    with ir.InsertionPoint(present):
                        arg0,  = present.arguments
                        if optype is IndexUnaryOp:
                            if op.thunk_as_index:
                                thunk_val = arith.ConstantOp(index, thunk._obj.item())
                            else:
                                thunk_val = arith.ConstantOp(thunk.dtype.build_mlir_type(), thunk._obj.item())
                            val = op(arg0, rowidx, colidx, thunk_val)
                        elif optype is BinaryOp:
                            if left is not None:
                                left_val = arith.ConstantOp(left.dtype.build_mlir_type(), left._obj.item())
                                val = op(left_val, arg0)
                            else:
                                right_val = arith.ConstantOp(right.dtype.build_mlir_type(), right._obj.item())
                                val = op(arg0, right_val)
                        else:
                            val = op(arg0)
                        sparse_tensor.YieldOp(result=val)
                    linalg.YieldOp([res])
                return generic_op.result
            main.func_op.attributes["llvm.emit_c_interface"] = ir.UnitAttr.get()

        return compile(module)


def _build_apply_inplace(op: Union[UnaryOp, BinaryOp],
                         sp: SparseTensorBase,
                         left: Optional[Scalar],
                         right: Optional[Scalar]):
    optype = type(op)
    op_result_dtype = op.get_output_type(sp.dtype)
    if op_result_dtype != sp.dtype:
        raise TypeError("apply inplace is restricted from changing dtype")

    with ir.Context(), ir.Location.unknown():
        module = ir.Module.create()
        with ir.InsertionPoint(module.body):
            index = ir.IndexType.get()
            dtype = sp.dtype.build_mlir_type()
            rtt = sp.rtt.as_mlir_type()
            memref_dtype = ir.MemRefType.get([ir.MemRefType.get_dynamic_size()], dtype)

            @func.FuncOp.from_py_func(rtt)
            def main(x):
                c0 = arith.ConstantOp(index, 0)
                c1 = arith.ConstantOp(index, 1)
                c_len = sparse_tensor.NumberOfEntriesOp(x)
                vals = sparse_tensor.ToValuesOp(memref_dtype, x)
                for_loop = scf.ForOp(c0, c_len, c1)
                with ir.InsertionPoint(for_loop.body):
                    x = for_loop.induction_variable
                    val = memref.LoadOp(vals, [x])
                    if optype is BinaryOp:
                        if left is not None:
                            left_val = arith.ConstantOp(left.dtype.build_mlir_type(), left._obj.item())
                            result = op(left_val, val)
                        else:
                            right_val = arith.ConstantOp(right.dtype.build_mlir_type(), right._obj.item())
                            result = op(val, right_val)
                    else:
                        result = op(val)
                    memref.StoreOp(result, vals, [x])
                    scf.YieldOp([])
            main.func_op.attributes["llvm.emit_c_interface"] = ir.UnitAttr.get()

        return compile(module)


def select(op: SelectOp, sp: SparseTensor, thunk: Scalar):
    rank = sp.ndims
    if rank == 0:  # Scalar
        # TODO: implement this
        raise NotImplementedError("doesn't yet work for Scalar")

    # Handle case of empty tensor
    if sp._obj is None:
        return sp.__class__(sp.dtype, sp.shape)

    # Build and compile if needed
    # Note that thunk is included in the key because it is inlined in the compiled code
    key = ('select', op.name, *sp.get_loop_key(), thunk._obj)
    if key not in engine_cache:
        engine_cache[key] = _build_select(op, sp, thunk)

    # Call the compiled function
    mem_out = get_sparse_output_pointer()
    arg_pointers = [sp._obj, mem_out]
    engine_cache[key].invoke('main', *arg_pointers)
    return sp.baseclass(sp.dtype, sp.shape, mem_out,
                            sp._sparsity, sp.perceived_ordering, intermediate_result=True)


def _build_select(op: SelectOp, sp: SparseTensorBase, thunk: Scalar):
    with ir.Context(), ir.Location.unknown():
        module = ir.Module.create()
        with ir.InsertionPoint(module.body):
            rank = sp.ndims
            index = ir.IndexType.get()
            i64 = ir.IntegerType.get_signless(64)
            dtype = sp.dtype.build_mlir_type()
            perm = ir.AffineMap.get_permutation(sp.permutation)
            perm_out = ir.AffineMap.get_permutation(range(rank))
            rtt = sp.rtt.as_mlir_type()
            rtt_out = sp.rtt.copy(ordering=sp.perceived_ordering).as_mlir_type()

            @func.FuncOp.from_py_func(rtt)
            def main(x):
                c = [arith.ConstantOp(index, i) for i in range(rank)]
                dims = [tensor.DimOp(x, c[i]).result for i in sp.permutation]
                out = bufferization.AllocTensorOp(rtt_out, dims, None, None, False)
                generic_op = linalg.GenericOp(
                    [rtt_out],
                    [x],
                    [out],
                    ir.ArrayAttr.get([ir.AffineMapAttr.get(p) for p in (perm, perm_out)]),
                    ir.ArrayAttr.get([ir.Attribute.parse('#linalg.iterator_type<parallel>')]*rank)
                )
                block = generic_op.regions[0].blocks.append(dtype, dtype)
                with ir.InsertionPoint(block):
                    a, o = block.arguments
                    rowidx = linalg.IndexOp(ir.IntegerAttr.get(i64, 0))
                    if rank == 2:
                        colidx = linalg.IndexOp(ir.IntegerAttr.get(i64, 1))
                    else:
                        colidx = arith.ConstantOp(index, 0)
                    res = sparse_tensor.SelectOp(a)
                    region = res.regions[0].blocks.append(dtype)
                    with ir.InsertionPoint(region):
                        arg0, = region.arguments
                        if op.thunk_as_index:
                            thunk_val = arith.ConstantOp(index, thunk._obj.item())
                        else:
                            thunk_val = arith.ConstantOp(thunk.dtype.build_mlir_type(), thunk._obj.item())
                        cmp = op(arg0, rowidx, colidx, thunk_val)
                        sparse_tensor.YieldOp(result=cmp)
                    linalg.YieldOp([res])
                return generic_op.result
            main.func_op.attributes["llvm.emit_c_interface"] = ir.UnitAttr.get()

        return compile(module)


def reduce_to_vector(op: Monoid, mat: Union[Matrix, TransposedMatrix]):
    # TODO: handle case of mat not having an _obj -> result will be empty vector

    # Build and compile if needed
    key = ('reduce_to_vector', op.name, *mat.get_loop_key())
    if key not in engine_cache:
        engine_cache[key] = _build_reduce_to_vector(op, mat)

    # Call the compiled function
    mem_out = get_sparse_output_pointer()
    arg_pointers = [mat._obj, mem_out]
    engine_cache[key].invoke('main', *arg_pointers)
    return Vector(mat.dtype, [mat.shape[0]], mem_out,
                  [DimLevelType.compressed], [0], intermediate_result=True)


def _build_reduce_to_vector(op: Monoid, mat: Union[Matrix, TransposedMatrix]):
    with ir.Context(), ir.Location.unknown():
        module = ir.Module.create()
        with ir.InsertionPoint(module.body):
            index = ir.IndexType.get()
            dtype = mat.dtype.build_mlir_type()
            perm = ir.AffineMap.get_permutation(mat.permutation)
            perm_out = ir.AffineMap.get(2, 0, [ir.AffineDimExpr.get(0)])
            rtt = mat.rtt.as_mlir_type()
            rtt_out = mat.rtt.copy(ordering=[0], sparsity=[DimLevelType.compressed]).as_mlir_type()

            @func.FuncOp.from_py_func(rtt)
            def main(x):
                c = [arith.ConstantOp(index, i) for i in range(2)]
                size = tensor.DimOp(x, c[mat.permutation[0]]).result
                out = bufferization.AllocTensorOp(rtt_out, [size], None, None, False)
                generic_op = linalg.GenericOp(
                    [rtt_out],
                    [x],
                    [out],
                    ir.ArrayAttr.get([ir.AffineMapAttr.get(p) for p in (perm, perm_out)]),
                    ir.ArrayAttr.get([ir.Attribute.parse('#linalg.iterator_type<parallel>'),
                                      ir.Attribute.parse('#linalg.iterator_type<reduction>')])
                )
                block = generic_op.regions[0].blocks.append(dtype, dtype)
                with ir.InsertionPoint(block):
                    a, o = block.arguments
                    ident = op.identity(mat.dtype)
                    res = sparse_tensor.ReduceOp(o, a, ident)
                    region = res.regions[0].blocks.append(dtype, dtype)
                    with ir.InsertionPoint(region):
                        arg0, arg1 = region.arguments
                        reduce_res = op.binop(arg0, arg1)
                        sparse_tensor.YieldOp(result=reduce_res)
                    linalg.YieldOp([res])
                return generic_op.result
            main.func_op.attributes["llvm.emit_c_interface"] = ir.UnitAttr.get()

        return compile(module)


def reduce_to_scalar(op: Monoid, sp: SparseTensorBase):
    # TODO: handle case of sp not having an _obj -> result will be empty scalar

    # Build and compile if needed
    key = ('reduce_to_scalar', op.name, *sp.get_loop_key())
    if key not in engine_cache:
        engine_cache[key] = _build_reduce_to_scalar(op, sp)

    # Call the compiled function
    mem_out = get_scalar_output_pointer(sp.dtype)
    arg_pointers = [sp._obj, mem_out]
    engine_cache[key].invoke('main', *arg_pointers)
    s = Scalar.new(sp.dtype)
    s.set_element(mem_out.contents.value)
    return s


def _build_reduce_to_scalar(op: Monoid, sp: SparseTensorBase):
    with ir.Context(), ir.Location.unknown():
        module = ir.Module.create()
        with ir.InsertionPoint(module.body):
            rank = sp.ndims
            index = ir.IndexType.get()
            dtype = sp.dtype.build_mlir_type()
            perm = ir.AffineMap.get_permutation(sp.permutation)
            perm_out = ir.AffineMap.get(rank, 0, [])
            rtt = sp.rtt.as_mlir_type()
            rtt_out = ir.RankedTensorType.get([], dtype)

            @func.FuncOp.from_py_func(rtt)
            def main(x):
                out = bufferization.AllocTensorOp(rtt_out, [], None, None, False)
                generic_op = linalg.GenericOp(
                    [rtt_out],
                    [x],
                    [out],
                    ir.ArrayAttr.get([ir.AffineMapAttr.get(p) for p in (perm, perm_out)]),
                    ir.ArrayAttr.get([ir.Attribute.parse('#linalg.iterator_type<reduction>')]*rank)
                )
                block = generic_op.regions[0].blocks.append(dtype, dtype)
                with ir.InsertionPoint(block):
                    a, o = block.arguments
                    ident = op.identity(sp.dtype)
                    res = sparse_tensor.ReduceOp(o, a, ident)
                    region = res.regions[0].blocks.append(dtype, dtype)
                    with ir.InsertionPoint(region):
                        arg0, arg1 = region.arguments
                        reduce_res = op.binop(arg0, arg1)
                        sparse_tensor.YieldOp(result=reduce_res)
                    linalg.YieldOp([res])
                s = tensor.ExtractOp(generic_op, [])
                return s.result
            main.func_op.attributes["llvm.emit_c_interface"] = ir.UnitAttr.get()

        return compile(module)
