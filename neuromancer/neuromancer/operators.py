import torch
import torch.nn as nn
import torch.nn.functional as F

from neuromancer.activations import soft_exp


class InterpolateAddMultiply(nn.Module):
    """
    Implementation of smooth interpolation between addition and multiplication
    using soft exponential activation: https://arxiv.org/pdf/1602.01321.pdf
    h(β, p, q) = f(β, f(−β, p) + f(−β, q)).
    """
    def __init__(self, alpha=0.0, tune_alpha=True):
        super().__init__()
        self.alpha = nn.Parameter(torch.tensor(alpha), requires_grad=tune_alpha)

    def forward(self, p, q):
        return soft_exp(self.alpha, soft_exp(-self.alpha, p) + soft_exp(-self.alpha, q))


operators = {'add': torch.add, 'mul': torch.mul, 'addmul': InterpolateAddMultiply()}

if __name__ == '__main__':
    x = torch.zeros(5, 10)
    y = torch.ones(5, 10)
    add = InterpolateAddMultiply()
    assert torch.equal(add(x, y), x + y)