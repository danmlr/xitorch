import sys
import inspect
import warnings
from abc import abstractmethod
from contextlib import contextmanager
import copy
import traceback as tb
import torch
from typing import Sequence, Union, Mapping, List, Dict
from lintorch.utils.exceptions import GetSetParamsError

__all__ = ["EditableModule"]

torch_float_type = [torch.float32, torch.float, torch.float64, torch.float16]

class EditableModule(object):
    @abstractmethod
    def getparams(self, methodname:str) -> Sequence[torch.Tensor]:
        """
        Returns a list of tensor parameters used in the object's operations
        """
        pass

    @abstractmethod
    def setparams(self, methodname:str, *params) -> int:
        """
        Set the input parameters to the object's parameters to make a copy of
        the operations.
        *params is an excessive list of the parameters to be set and the
        method will return the number of parameters it sets.
        """
        pass

    def getuniqueparams(self, methodname:str) -> Sequence[torch.Tensor]:
        allparams = self.getparams(methodname)
        idxs = self._get_unique_params_idxs(methodname, allparams)
        return [allparams[i] for i in idxs]

    def setuniqueparams(self, methodname:str, *uniqueparams) -> int:
        nparams = self._number_of_params[methodname]
        allparams = [None for _ in range(nparams)]
        maps = self._unique_params_maps[methodname]

        for j in range(len(uniqueparams)):
            jmap = maps[j]
            p = uniqueparams[j]
            for i in jmap:
                allparams[i] = p

        return self.setparams(methodname, *allparams)

    def _get_unique_params_idxs(self, methodname:str,
            allparams:Union[Sequence[torch.Tensor],None]=None) -> Sequence[int]:

        if not hasattr(self, "_unique_params_idxs"):
            self._unique_params_idxs = {} # type: Dict[str,List[int]]
            self._unique_params_maps = {}
            self._number_of_params = {}

        if methodname in self._unique_params_idxs:
            return self._unique_params_idxs[methodname]
        if allparams is None:
            allparams = self.getparams(methodname)

        # get the unique ids
        ids = [] # type: List[int]
        idxs = []
        idx_map = [] # type: List[List[int]]
        for i in range(len(allparams)):
            param = allparams[i]
            id_param = id(param)

            # search the id if it has been added to the list
            try:
                jfound = ids.index(id_param)
                idx_map[jfound].append(i)
                continue
            except ValueError:
                pass

            ids.append(id_param)
            idxs.append(i)
            idx_map.append([i])

        self._number_of_params[methodname] = len(allparams)
        self._unique_params_idxs[methodname] = idxs
        self._unique_params_maps[methodname] = idx_map
        return idxs

    @contextmanager
    def useparams(self, methodname:str, *params):
        try:
            _orig_params_ = self.getuniqueparams(methodname)
            self.setuniqueparams(methodname, *params)
            yield self
        except Exception as exc:
            tb.print_exc()
        finally:
            self.setuniqueparams(methodname, *_orig_params_)

    ############# debugging #############
    def assertparams(self, methodname, *args, **kwargs):
        """
        Perform a rigorous check on the implemented getparams and setparams
        in the class for a given method and its arguments (as well as keyword
        arguments)
        """
        method = getattr(self, methodname)

        self.__assert_method_preserve(method, *args, **kwargs) # assert if the method preserve the float tensors of the object
        self.__assert_match_getsetparams(methodname) # check if getparams and setparams matched the tensors
        self.__assert_get_correct_params(method, *args, **kwargs) # check if getparams returns the correct tensors

    def __assert_method_preserve(self, method, *args, **kwargs):
        # this method assert if method does not change the float tensor parameters
        # of the object (i.e. it preserves the state of the object)

        all_params0, names0 = _get_tensors(self)
        all_params0 = [p.clone() for p in all_params0]
        method(*args, **kwargs)
        all_params1, names1 = _get_tensors(self)

        # now assert if all_params0 == all_params1
        clsname = method.__self__.__class__.__name__
        methodname = method.__name__
        msg = "The method %s.%s does not preserve the object's float tensors" % (clsname, methodname)
        if len(all_params0) != len(all_params1):
            raise GetSetParamsError(msg)

        for p0,p1 in zip(all_params0, all_params1):
            if p0.shape != p1.shape:
                raise GetSetParamsError(msg)
            if not torch.allclose(p0,p1):
                raise GetSetParamsError(msg)

    def __assert_match_getsetparams(self, methodname):
        # this function assert if get & set params functions correspond to the
        # same parameters in the same order

        # count the number of parameters in getparams and setparams
        params0 = self.getparams(methodname)
        len_setparams0 = self.setparams(methodname, *params0)
        if len_setparams0 != len(params0):
            raise GetSetParamsError("The number of parameters returned by getparams and set by setparams do not match \n"\
                "(getparams: %d, setparams: %d)" % (len(params0), len_setparams0))

        # check if the params are assigned correctly in the correct order
        params1 = self.getparams(methodname)
        for i,p0,p1 in zip(range(len(params0)), params0, params1):
            if id(p0) != id(p1):
                msg = "The parameter #%d in getparams and setparams does not match\n" % i
                msg += self.__get_error_message_ith_params(methodname, params1, i)
                raise GetSetParamsError(msg)

    def __assert_get_correct_params(self, method, *args, **kwargs):
        # this function perform checks if the getparams on the method returns
        # the correct tensors

        methodname = method.__name__
        clsname = method.__self__.__class__.__name__

        # get all tensor parameters in the object
        all_params, all_names = _get_tensors(self)
        def _get_tensor_name(param):
            for i in range(len(all_params)):
                if id(all_params[i]) == id(param):
                    return all_names[i]
            return None

        # get the parameter tensors used in the operation and the tensors specified by the developer
        oper_names, oper_params = self.__list_operating_params(method, *args, **kwargs)
        user_params = self.getparams(method.__name__)
        id_operparams = [id(p) for p in oper_params]
        id_userparams = [id(p) for p in user_params]

        # check if the userparams contains non-tensor
        for i in range(len(user_params)):
            param = user_params[i]
            if (not isinstance(param, torch.Tensor)) or (isinstance(param, torch.Tensor) and param.dtype not in torch_float_type):
                msg = "Non-floating point tensor param is detected at position #%d (type: %s).\n" % (i, type(param))
                msg += self.__get_error_message_ith_params(methodname, user_params, i)
                raise GetSetParamsError(msg)

        # check if there are missing parameters (present in operating params, but not in the user params)
        missing_names = []
        for i in range(len(oper_params)):
            if id_operparams[i] not in id_userparams:
                missing_names.append(oper_names[i])
        # if there are missing parameters, give a warning (because the program
        # can still run correctly, e.g. missing parameters are parameters that
        # are never set to require grad)
        if len(missing_names) > 0:
            msg = "getparams for %s.%s does not include: %s" % (clsname, methodname, ", ".join(missing_names))
            warnings.warn(msg)

        # check if there are excessive parameters (present in the user params, but not in the operating params)
        excess_names = []
        for i in range(len(user_params)):
            if id_userparams[i] not in id_operparams:
                name = _get_tensor_name(user_params[i])
                # if name is None, it means the getparams returns parameters that
                # are not tensors or not a member of the class
                if name is None:
                    msg = "The parameter #%d in getparams is not a float tensor member of the class\n"
                    msg += self.__get_error_message_ith_params(methodname, user_params, i)
                    raise GetSetParamsError(msg)
                else:
                    excess_names.append(name)
        # if there are excess parameters, raise an error because it can cause
        # infinite loop in backward operation of some functions
        # (some backward functions are usually recursive and pytorch backward
        # process will travel backward in the graph until it finds the parameters,
        # so if there are excess parameters, the process will travel backward
        # indefinitely)
        if len(excess_names) > 0:
            raise GetSetParamsError("getparams for %s.%s has excess parameters: %s" % \
                (clsname, methodname, ", ".join(excess_names)))

    def __list_operating_params(self, method, *args, **kwargs):
        """
        List the tensors used in executing the method by calling the method
        and see which parameters are connected in the backward graph
        """
        # get all the tensors recursively
        all_tensors, all_names = _get_tensors(self)

        # copy the tensors and require them to be differentiable
        copy_tensors0 = [tensor.clone().detach().requires_grad_() for tensor in all_tensors]
        copy_tensors = copy.copy(copy_tensors0)
        _set_tensors(self, copy_tensors)

        # run the method and see which one has the gradients
        output = method(*args, **kwargs).sum()
        grad_tensors = torch.autograd.grad(output, copy_tensors0, allow_unused=True)

        # return the original tensor
        all_tensors_copy = copy.copy(all_tensors)
        _set_tensors(self, all_tensors_copy)

        names = []
        params = []
        for i, grad in enumerate(grad_tensors):
            if grad is None:
                continue
            names.append(all_names[i])
            params.append(all_tensors[i])

        return names, params

    def __get_error_message_ith_params(self, methodname, params, i):
        # return the message indicating where the i-th parameter is
        msg = "The position of the parameter #%d (0-based) can be detected using setparams as below:\n" % i
        msg += "--------\n"
        try:
            self.setparams(methodname, *params[:i])
        except:
            s = tb.format_exc()
            msg += s
        return msg

def getmethodparams(method):
    if not inspect.ismethod(method):
        return []
    obj = method.__self__
    methodname = method.__name__
    if not isinstance(obj, EditableModule):
        return []
    return obj.getparams(methodname)

def setmethodparams(method, *params):
    if not inspect.ismethod(method):
        return
    obj = method.__self__
    methodname = method.__name__
    if not isinstance(obj, EditableModule):
        return 0
    return obj.setparams(methodname, *params)

############################ traversing functions ############################
def _traverse_obj(obj, prefix, action, crit, max_depth=20, exception_ids=None):
    """
    Traverse an object to get/set variables that are accessible through the object.
    """
    if exception_ids is None:
        # None is set as default arg to avoid expanding list for multiple
        # invokes of _get_tensors without exception_ids argument
        exception_ids = []

    if hasattr(obj, "__dict__"):
        generator = obj.__dict__.items()
        name_format = "{prefix}.{key}"
        objdict = obj.__dict__
    elif hasattr(obj, "__iter__"):
        generator = enumerate(obj)
        name_format = "{prefix}[{key}]"
        objdict = obj
    else:
        raise RuntimeError("The object must be iterable or keyable")

    for key,elmt in generator:
        if id(elmt) in exception_ids:
            continue
        else:
            exception_ids.append(id(elmt))

        name = name_format.format(prefix=prefix, key=key)
        if crit(elmt):
            action(elmt, name, objdict, key)
        elif hasattr(elmt, "__dict__") or hasattr(elmt, "__iter__"):
            if max_depth > 0:
                _traverse_obj(elmt, action=action, prefix=name, max_depth=max_depth-1, exception_ids=exception_ids)
            else:
                raise RecursionError("Maximum number of recursion reached")

def _get_tensors(obj, prefix="self", max_depth=20):
    """
    Collect all tensors in an object recursively and return the tensors as well
    as their "names" (names meaning the address, e.g. "self.a[0].elmt").

    Arguments
    ---------
    * obj: an instance
        The object user wants to traverse down
    * prefix: str
        Prefix of the name of the collected tensors. Default: "self"

    Returns
    -------
    * res: list of torch.Tensor
        List of tensors collected recursively in the object.
    * name: list of str
        List of names of the collected tensors.
    """

    # get the tensors recursively towards torch.nn.Module
    res = []
    names = []
    def action(elmt, name, objdict, key):
        res.append(elmt)
        names.append(name)

    # traverse down the object to collect the tensors
    crit = lambda elmt: isinstance(elmt, torch.Tensor) and elmt.dtype in torch_float_type
    _traverse_obj(obj, action=action, crit=crit, prefix=prefix, max_depth=max_depth)
    return res, names

def _set_tensors(obj, all_params, max_depth=20):
    """
    Set the tensors in an object to new tensor object listed in `all_params`.

    Arguments
    ---------
    * obj: an instance
        The object user wants to traverse down
    * all_params: list of torch.Tensor
        List of tensors to be put in the object.
    * max_depth: int
        Maximum recursive depth to avoid infinitely running program.
        If the maximum depth is reached, then raise a RecursionError.
    """
    def action(elmt, name, objdict, key):
        objdict[key] = all_params.pop(0)
    # traverse down the object to collect the tensors
    crit = lambda elmt: isinstance(elmt, torch.Tensor) and elmt.dtype in torch_float_type
    _traverse_obj(obj, action=action, crit=crit, prefix="self", max_depth=max_depth)
