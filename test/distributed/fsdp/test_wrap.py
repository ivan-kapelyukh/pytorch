# Owner(s): ["oncall: distributed"]

from enum import Enum, auto
import functools
import os
import unittest

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributed._fsdp.fully_sharded_data_parallel import (
    FullyShardedDataParallel as FSDP,
    CPUOffload,
)
from torch.distributed._fsdp.wrap import (
    auto_wrap,
    default_auto_wrap_policy,
    enable_wrap,
    wrap,
)
from torch.testing._internal.common_distributed import (
    skip_if_lt_x_gpu,
)
from torch.testing._internal.common_fsdp import (
    DummyProcessGroup,
    FSDPTest,
    FSDPInitMode,
    _maybe_cuda,
)
from torch.testing._internal.common_utils import (
    run_tests,
    find_free_port,
    TestCase,
    parametrize,
    instantiate_parametrized_tests,
)

class WrapMethod(Enum):
    FSDP_CTOR = auto()
    # FSDP_CTOR is the supported way forward, but keep WRAP_API in case we miss
    # any use cases and fix them to work with FSDP_CTOR over time.
    WRAP_API = auto()




class TestFSDPWrap(FSDPTest):
    """
    Tests main API for wrapping FSDP, which is to pass auto_wrap_policy into
    FSDP constructor.
    """

    def setUp(self) -> None:
        super().setUp()
        torch.cuda.set_device(self.rank)

    class NestedSequentialModel:
        @staticmethod
        def get_model(cuda=True):
            sequential = nn.Sequential(
                nn.Linear(5, 5),
                nn.Linear(5, 5),
                nn.Sequential(nn.Linear(5, 5), nn.Linear(5, 5)),
            )
            if cuda:
                sequential = sequential.cuda()
            return sequential

        @staticmethod
        def verify_model(cls, model):
            cls.assertTrue(isinstance(model, FSDP))
            cls.assertTrue(isinstance(model.module[0], nn.Linear))
            cls.assertTrue(isinstance(model.module[1], nn.Linear))
            cls.assertTrue(isinstance(model.module[2], FSDP))
            cls.assertTrue(isinstance(model.module[2].module[0], nn.Linear))
            cls.assertTrue(isinstance(model.module[2].module[1], nn.Linear))

    def _get_linear(self, fin, fout):
        return nn.Linear(fin, fout, bias=False)

    def _get_already_wrapped_fsdp(
        self, fsdp_init_mode=FSDPInitMode.CUDA_BEFORE, nested=False
    ) -> FSDP:
        fn_self = self

        class MyModel(nn.Module):
            def __init__(self, nested):
                super().__init__()
                # TODO: test the various init modes.
                move_to_cuda = fsdp_init_mode == FSDPInitMode.CUDA_BEFORE
                # if nested=True, the FSDP module will be nested one layer deep
                # and we should pick that up.
                if nested:
                    self.lin1 = nn.Sequential(
                        _maybe_cuda(fn_self._get_linear(1, 1), move_to_cuda),
                        FSDP(_maybe_cuda(fn_self._get_linear(1, 1), move_to_cuda)),
                    )
                else:
                    self.lin1 = FSDP(
                        _maybe_cuda(fn_self._get_linear(1, 1), move_to_cuda)
                    )
                self.lin2 = FSDP(_maybe_cuda(fn_self._get_linear(1, 1), move_to_cuda))
                self.lin3 = FSDP(_maybe_cuda(fn_self._get_linear(1, 1), move_to_cuda))

            def forward(self, input: torch.Tensor) -> torch.Tensor:
                return self.lin3(self.lin2(self.lin1(input)))

        model = MyModel(nested=nested)
        return model

    @skip_if_lt_x_gpu(2)
    @parametrize("nested", [True, False])
    @parametrize(
        "cpu_offload",
        [CPUOffload(offload_params=True), CPUOffload(offload_params=False)]
    )
    def test_error_auto_wrap(self, nested):
        wrapped_fsdp = self._get_already_wrapped_fsdp(nested=nested)
        with self.assertRaisesRegex(ValueError, "to NOT be FullyShardedDataParallel"):
            mod = FSDP(wrapped_fsdp, fsdp_auto_wrap_policy=default_auto_wrap_policy)

    @skip_if_lt_x_gpu(2)
    @parametrize(
        "cpu_offload",
        [CPUOffload(offload_params=True), CPUOffload(offload_params=False)]
    )
    def test_main_api_auto_wrap(self, cpu_offload):

        class Nested(nn.Module):
            def __init__(self):
                super().__init__()
                self.nested_lin = nn.Linear(1, 1, bias=False).cuda()

            def forward(self, input):
                return self.nested_lin(input)

        class MyModel(nn.Module):
            def __init__(self):
                super().__init__()
                self.lin1 = nn.Linear(1, 1, bias=False).cuda()
                self.lin2 = nn.Linear(1, 1, bias=False).cuda()
                self.lin3 = nn.Linear(1, 1, bias=False).cuda()
                self.lin4 = Nested().cuda()

            def forward(self, input):
                return self.lin4(self.lin3(self.lin2(self.lin1(input))))

        model = MyModel()
        wrapped_model = FSDP(
            model,
            fsdp_auto_wrap_policy=functools.partial(
                default_auto_wrap_policy,
                min_num_params=0,  # wrap all modules
            ),
            cpu_offload=cpu_offload,
        )
        modules = [
            wrapped_model,
            wrapped_model.module.lin1,
            wrapped_model.module.lin2,
            wrapped_model.module.lin3,
            wrapped_model.module.lin4,
            # Nested FSDP
            wrapped_model.module.lin4.module.nested_lin,
        ]
        for module in modules:
            self.assertTrue(isinstance(module, FSDP))
            self._check_cpu_offload(module, cpu_offload)
        # Run model a few times for sanity check.
        optim = torch.optim.SGD(model.parameters(), lr=1e-2, momentum=0.9)
        inp = torch.ones(1).cuda()
        for _ in range(6):
            optim.zero_grad()
            loss = wrapped_model(inp).sum()
            loss.backward()
            optim.step()


class TestAutoWrap(TestCase):
    def setUp(self) -> None:
        super().setUp()
        # For all the tests here, we use a fake group
        self.process_group = DummyProcessGroup(rank=0, size=1)

    @parametrize("wrap_method", [WrapMethod.FSDP_CTOR, WrapMethod.WRAP_API])
    def test_wrap(self, wrap_method):
        if wrap_method == WrapMethod.WRAP_API:
            with enable_wrap(wrapper_cls=FSDP, process_group=self.process_group):
                layer = wrap(nn.Linear(5, 5))
        else:
            assert wrap_method == WrapMethod.FSDP_CTOR
            layer = FSDP(
                nn.Linear(5, 5),
                process_group=self.process_group,
                fsdp_auto_wrap_policy=functools.partial(default_auto_wrap_policy, min_num_params=1)
            )
        self.assertTrue(isinstance(layer, FSDP))
        self.assertEqual(layer.rank, self.process_group.rank())
        self.assertEqual(layer.world_size, self.process_group.size())

    def test_wrap_disabled_outside_context(self):
        layer = wrap(nn.Linear(5, 5))
        self.assertTrue(isinstance(layer, nn.Linear))

    def test_wrap_override_defaults(self):
        new_process_group = DummyProcessGroup(rank=0, size=2)
        with enable_wrap(wrapper_cls=FSDP, process_group=self.process_group):
            layer = wrap(nn.Linear(5, 5), process_group=new_process_group)
        self.assertTrue(isinstance(layer, FSDP))
        self.assertEqual(layer.rank, 0)
        self.assertEqual(layer.world_size, 2)

    @parametrize("wrap_method", [WrapMethod.FSDP_CTOR, WrapMethod.WRAP_API])
    def test_auto_wrap_foo(self, wrap_method):
        """
        Test to ensure with auto wrap, we wrap child modules correctly based on the min_num_params.
        ``nn.Linear(5, 5)`` does not exceed the bucket size, but combined they do.
        """
        sequential = TestFSDPWrap.NestedSequentialModel.get_model(cuda=False)
        my_auto_wrap_policy = functools.partial(
            default_auto_wrap_policy, min_num_params=40
        )
        if wrap_method == WrapMethod.WRAP_API:
            with enable_wrap(wrapper_cls=FSDP, process_group=self.process_group):
                model = auto_wrap(sequential, auto_wrap_policy=my_auto_wrap_policy)
        else:
            assert wrap_method == WrapMethod.FSDP_CTOR
            model = FSDP(sequential, process_group=self.process_group, fsdp_auto_wrap_policy=my_auto_wrap_policy)

        TestFSDPWrap.NestedSequentialModel.verify_model(self, model)


    def test_auto_wrap_preset_exclude_wrap(self):
        """
        Test to ensure excluded modules are not wrapped, regardless if the total param size is greater than the
        min_num_params. the default_auto_wrap_policy excludes wrapping for {nn.ModuleList, nn.ModuleDict}
        """
        with enable_wrap(wrapper_cls=FSDP, process_group=self.process_group):
            sequential = nn.ModuleList([nn.Linear(5, 5), nn.Linear(5, 5)])
            my_auto_wrap_policy = functools.partial(
                default_auto_wrap_policy, min_num_params=40
            )
            model = auto_wrap(sequential, auto_wrap_policy=my_auto_wrap_policy)
        self.assertTrue(isinstance(model, nn.ModuleList))
        self.assertTrue(isinstance(model[0], nn.Linear))
        self.assertTrue(isinstance(model[1], nn.Linear))

    def test_auto_wrap_preset_exclude_wrap_include_children(self):
        """
        Test to ensure excluded modules are not wrapped, but children are if param size is greater than
        min_num_params
        """
        with enable_wrap(wrapper_cls=FSDP, process_group=self.process_group):
            sequential = nn.ModuleList([nn.Linear(10, 10)])
            my_auto_wrap_policy = functools.partial(
                default_auto_wrap_policy, min_num_params=40
            )
            model = auto_wrap(sequential, auto_wrap_policy=my_auto_wrap_policy)
        self.assertTrue(isinstance(model, nn.ModuleList))
        self.assertTrue(isinstance(model[0], FSDP))

    def test_auto_wrap_preset_force_leaf(self):
        """
        Test to ensure force-leaf modules are not wrapped, and children are not wrapped. The
        default_auto_wrap_policy forces leaf modules of type {nn.MultiheadAttention} to not be wrapped
        """
        with enable_wrap(wrapper_cls=FSDP, process_group=self.process_group):
            sequential = nn.Sequential(nn.Linear(10, 10), nn.MultiheadAttention(100, 1))
            my_auto_wrap_policy = functools.partial(
                default_auto_wrap_policy, min_num_params=40
            )
            model = auto_wrap(sequential, auto_wrap_policy=my_auto_wrap_policy)
        self.assertTrue(isinstance(model.module[0], FSDP))
        # Assert children of multihead attention are not wrapped
        self.assertTrue(isinstance(model.module[1], nn.MultiheadAttention))
        self.assertTrue(isinstance(model.module[1].out_proj, nn.Linear))

    def test_auto_wrap_preset_force_leaf_custom(self):
        """
        Test to ensure force-leaf modules are not wrapped.
        """
        my_auto_wrap_policy = functools.partial(
            default_auto_wrap_policy,
            min_num_params=40,
            force_leaf_modules=default_auto_wrap_policy.FORCE_LEAF_MODULES.union(
                {nn.Linear}
            ),
        )
        with enable_wrap(
            auto_wrap_policy=my_auto_wrap_policy,
            wrapper_cls=FSDP,
            process_group=self.process_group,
        ):
            sequential = nn.Sequential(
                nn.Linear(10, 10), nn.ModuleList([nn.Linear(10, 10)])
            )
            model = auto_wrap(sequential)
        # Model was wrapped in FSDP as no inner modules were wrapped.
        self.assertTrue(isinstance(model, FSDP))
        self.assertTrue(isinstance(model.module[0], nn.Linear))
        self.assertTrue(isinstance(model.module[1], nn.ModuleList))

    @unittest.skipIf(not torch.cuda.is_available(), "Test Requires CUDA")
    @parametrize("wrap_method", [WrapMethod.FSDP_CTOR, WrapMethod.WRAP_API])
    def test_auto_wrap_smoke_test(self, wrap_method):
        device = torch.device("cuda")
        torch.cuda.set_device(0)

        # Random port in case the next test run quickly, same port would cause conflict.
        os.environ["MASTER_ADDR"] = "localhost"
        os.environ["MASTER_PORT"] = str(find_free_port())
        torch.distributed.init_process_group(backend="nccl", rank=0, world_size=1)


        try:
            sequential = TestFSDPWrap.NestedSequentialModel.get_model()
            my_auto_wrap_policy = functools.partial(
                default_auto_wrap_policy, min_num_params=40
            )
            if wrap_method == WrapMethod.WRAP_API:
                with enable_wrap(wrapper_cls=FSDP):
                    model = auto_wrap(sequential, auto_wrap_policy=my_auto_wrap_policy)
            else:
                model = FSDP(sequential, fsdp_auto_wrap_policy=my_auto_wrap_policy)

            TestFSDPWrap.NestedSequentialModel.verify_model(self, model)
            input = torch.rand((1, 5), dtype=torch.float).to(device)
            output = model(input)
            loss = F.mse_loss(input, output)
            loss.backward()
        finally:
            torch.distributed.destroy_process_group()
            del os.environ["MASTER_ADDR"]
            del os.environ["MASTER_PORT"]


instantiate_parametrized_tests(TestFSDPWrap)
instantiate_parametrized_tests(TestAutoWrap)

if __name__ == "__main__":
    run_tests()
