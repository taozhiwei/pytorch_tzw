# Owner(s): ["oncall: pt2"]
import dataclasses
import functools

import torch
from torch import nn
from torch._dynamo import compiled_autograd
from torch._dynamo.test_case import run_tests, TestCase
from torch._dynamo.testing import CompileCounter
from torch.testing._internal.common_utils import IS_MACOS
from torch.testing._internal.inductor_utils import HAS_CPU


def init_module_bw_hooks(allow_eager):
    def bw_pre_hook(mod, gO):
        assert allow_eager or torch._dynamo.is_compiling()
        assert mod.weight.size() == (10, 10)
        mod.hook_count_pre.add_(1)
        return (torch.sin(gO[0] + 1.2),)

    def bw_post_hook(mod, gI, gO):
        assert allow_eager or torch._dynamo.is_compiling()
        assert mod.weight.size() == (10, 10)
        mod.hook_count_post.add_(1)
        return (torch.sin(gI[0] + 3.4),)

    torch.manual_seed(1234)
    m = nn.Linear(10, 10)
    m.hook_count_pre = torch.tensor(0)
    m.hook_count_post = torch.tensor(0)
    m.register_full_backward_pre_hook(bw_pre_hook)
    m.register_full_backward_hook(bw_post_hook)
    return m, torch.rand(2, 10, requires_grad=True)


def steps(m, inp):
    for _ in range(4):
        out = m(inp)
        out.sum().backward()
    return out


class DistributedPatternTests(TestCase):
    def test_intermediate_hook_with_closure(self):
        @dataclasses.dataclass
        class CustomObj:
            val: torch.Tensor

        def fn(x, obj):
            y = x.sin()
            closure_var = y + 1
            y.register_hook(lambda grad: grad + obj.val + closure_var)
            z = y.sin()
            return z

        opt = torch.compile(fn, fullgraph=True)

        obj1 = CustomObj(torch.tensor(88))
        obj2 = CustomObj(torch.tensor(99))
        x0 = torch.ones(4, requires_grad=True)
        x1 = torch.ones(4, requires_grad=True)
        x2 = torch.ones(4, requires_grad=True)
        x3 = torch.ones(4, requires_grad=True)
        fn(x0, obj1).sum().backward()
        fn(x1, obj2).sum().backward()

        with compiled_autograd.enable(functools.partial(torch.compile, fullgraph=True)):
            opt(x2, obj1).sum().backward()
            opt(x3, obj2).sum().backward()

        self.assertEqual(x0.grad, x2.grad)
        self.assertEqual(x1.grad, x3.grad)

    @torch.no_grad()
    def test_storage_resize_zero(self):
        @torch.compile(fullgraph=True)
        def fn(x):
            y = torch.sin(x)
            x.untyped_storage().resize_(0)
            return torch.cos(y)

        x = torch.randn(10)
        expected = torch.cos(torch.sin(x))
        y = fn(x)
        self.assertEqual(y, expected)
        self.assertEqual(x.untyped_storage().size(), 0)

    @torch.no_grad()
    def test_storage_resize_nonzero(self):
        @torch.compile(fullgraph=True)
        def fn(x, out):
            y = torch.sin(x)
            assert out.untyped_storage().size() == 0
            out.untyped_storage().resize_(x.untyped_storage().size())
            out.copy_(y.cos())

        x = torch.randn(10)
        out = torch.randn(10)
        expected = torch.cos(torch.sin(x))
        out.untyped_storage().resize_(0)
        fn(x, out)
        self.assertEqual(out.untyped_storage().size(), x.untyped_storage().size())
        self.assertEqual(out, expected)

    def test_module_backward_hooks_eager(self):
        m1, inp1 = init_module_bw_hooks(True)
        out1 = steps(m1, inp1)

        m2, inp2 = init_module_bw_hooks(False)
        fw_cnt = CompileCounter()
        bw_cnt = CompileCounter()
        with compiled_autograd.enable(torch.compile(backend=bw_cnt, fullgraph=True)):
            m2 = torch.compile(m2, backend=fw_cnt, fullgraph=True)
            out2 = steps(m2, inp2)

        self.assertEqual(m1.hook_count_pre, m2.hook_count_pre)
        self.assertEqual(m1.hook_count_post, m2.hook_count_post)
        self.assertEqual(out1, out2)
        self.assertEqual(inp1.grad, inp2.grad)
        self.assertEqual(m1.weight.grad, m2.weight.grad)
        self.assertEqual(m1.bias.grad, m2.bias.grad)

        self.assertEqual(fw_cnt.frame_count, 1)
        self.assertEqual(fw_cnt.op_count, 5)
        self.assertEqual(bw_cnt.frame_count, 2)  # grad=None and grad!=None
        self.assertEqual(bw_cnt.op_count, 39)

    def test_module_backward_hooks_aot(self):
        m1, inp1 = init_module_bw_hooks(True)
        out1 = steps(m1, inp1)

        m2, inp2 = init_module_bw_hooks(True)
        m2 = torch.compile(m2, backend="aot_eager", fullgraph=True)
        with compiled_autograd.enable(lambda gm: gm):
            out2 = steps(m2, inp2)

        self.assertEqual(m1.hook_count_pre, m2.hook_count_pre)
        self.assertEqual(m1.hook_count_post, m2.hook_count_post)
        self.assertEqual(out1, out2)
        self.assertEqual(inp1.grad, inp2.grad)
        self.assertEqual(m1.weight.grad, m2.weight.grad)
        self.assertEqual(m1.bias.grad, m2.bias.grad)

    def test_module_backward_hooks_inductor(self):
        m1, inp1 = init_module_bw_hooks(True)
        out1 = steps(m1, inp1)

        m2, inp2 = init_module_bw_hooks(False)
        m2 = torch.compile(m2, fullgraph=True)
        with compiled_autograd.enable(torch.compile(fullgraph=True)):
            out2 = steps(m2, inp2)

        self.assertEqual(m1.hook_count_pre, m2.hook_count_pre)
        self.assertEqual(m1.hook_count_post, m2.hook_count_post)
        self.assertEqual(out1, out2)
        self.assertEqual(inp1.grad, inp2.grad)
        self.assertEqual(m1.weight.grad, m2.weight.grad)
        self.assertEqual(m1.bias.grad, m2.bias.grad)

    def test_module_backward_hooks_multi_layers(self):
        a1, inp1 = init_module_bw_hooks(True)
        b1, _ = init_module_bw_hooks(True)
        out1 = steps(torch.nn.Sequential(a1, b1), inp1)

        a2, inp2 = init_module_bw_hooks(False)
        b2, _ = init_module_bw_hooks(False)
        with compiled_autograd.enable(torch.compile(fullgraph=True)):
            out2 = steps(
                torch.compile(torch.nn.Sequential(a2, b2), fullgraph=True), inp2
            )

        self.assertEqual(a1.hook_count_pre, a2.hook_count_pre)
        self.assertEqual(a1.hook_count_post, a2.hook_count_post)
        self.assertEqual(b1.hook_count_pre, b2.hook_count_pre)
        self.assertEqual(b1.hook_count_post, b2.hook_count_post)
        self.assertEqual(out1, out2)
        self.assertEqual(inp1.grad, inp2.grad)
        self.assertEqual(a1.weight.grad, a2.weight.grad)
        self.assertEqual(a1.bias.grad, a2.bias.grad)
        self.assertEqual(b1.weight.grad, b2.weight.grad)
        self.assertEqual(b1.bias.grad, b2.bias.grad)

    # TODO(jansel): support bw hooks with graph break


if __name__ == "__main__":
    if HAS_CPU and not IS_MACOS:
        run_tests(needs="filelock")
