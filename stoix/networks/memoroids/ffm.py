from typing import Tuple

import chex
import jax
from flax import linen as nn
from jax import numpy as jnp

from stoix.networks.memoroids.base import (
    InputEmbedding,
    Inputs,
    RecurrentState,
    Reset,
    ScanInput,
    Timestep,
)


def init_deterministic_a(
    memory_size: int,
) -> Tuple[chex.Array, chex.Array]:
    def init(key, shape):
        a_low = 1e-6
        a_high = 0.5
        a = jnp.linspace(a_low, a_high, memory_size)
        return a

    return init


def init_deterministic_b(
    context_size: int, min_period: int = 1, max_period: int = 1_000
) -> Tuple[chex.Array, chex.Array]:
    def init(key, shape):
        b = 2 * jnp.pi / jnp.linspace(min_period, max_period, context_size)
        return b

    return init


class Gate(nn.Module):
    output_size: int

    @nn.compact
    def __call__(self, x: chex.Array) -> chex.Array:
        return jax.nn.sigmoid(nn.Dense(self.output_size)(x))


class FFMCell(nn.Module):
    trace_size: int
    context_size: int
    output_size: int

    def map_to_h(self, x: InputEmbedding) -> ScanInput:
        """Given an input embedding, this will map it to the format required for the associative scan."""
        gate_in = Gate(self.trace_size)(x)
        pre = nn.Dense(self.trace_size)(x)
        gated_x = pre * gate_in
        scan_input = jnp.repeat(jnp.expand_dims(gated_x, 3), self.context_size, axis=3)
        return scan_input

    def map_from_h(self, state: RecurrentState, x: InputEmbedding) -> chex.Array:
        """Given the recurrent state and the input embedding, this will map the recurrent state back to the output space."""
        T = state.shape[0]
        B = state.shape[1]
        z_in = jnp.concatenate([jnp.real(state), jnp.imag(state)], axis=-1).reshape(T, B, -1)
        z = nn.Dense(self.output_size)(z_in)
        gate_out = Gate(self.output_size)(x)
        skip = nn.Dense(self.output_size)(x)
        out = nn.LayerNorm(use_scale=False, use_bias=False)(z * gate_out) + skip * (1 - gate_out)
        return out

    def log_gamma(self, t: Timestep) -> chex.Array:
        T = t.shape[0]
        B = t.shape[1]

        a = self.param(
            "ffm_a",
            init_deterministic_a(self.trace_size),
            (),
        )
        b = self.param(
            "ffm_b",
            init_deterministic_b(self.context_size),
            (),
        )
        a = -jnp.abs(a).reshape((1, 1, self.trace_size, 1))
        b = b.reshape(1, 1, 1, self.context_size)
        ab = jax.lax.complex(a, b)
        return ab * t.reshape(T, B, 1, 1)

    def gamma(self, t: Timestep) -> chex.Array:
        return jnp.exp(self.log_gamma(t))

    def unwrapped_associative_update(
        self,
        carry: Tuple[RecurrentState, Timestep],
        incoming: Tuple[InputEmbedding, Timestep],
    ) -> Tuple[RecurrentState, Timestep]:
        (
            state,
            i,
        ) = carry
        x, j = incoming
        state = state * self.gamma(j) + x
        return state, j + i

    def wrapped_associative_update(
        self,
        carry: Tuple[Reset, RecurrentState, Timestep],
        incoming: Tuple[Reset, InputEmbedding, Timestep],
    ) -> Tuple[Reset, RecurrentState, Timestep]:
        prev_start, state, i = carry
        start, x, j = incoming
        # Reset all elements in the carry if we are starting a new episode
        state = state * jnp.logical_not(start)
        j = j * jnp.logical_not(start)
        incoming = x, j
        carry = (state, i)
        out = self.unwrapped_associative_update(carry, incoming)
        start_out = jnp.logical_or(start, prev_start)
        return (start_out, *out)

    def scan(
        self,
        x: InputEmbedding,
        state: RecurrentState,
        start: Reset,
    ) -> RecurrentState:
        """Given an input and recurrent state, this will update the recurrent state. This is equivalent
        to the inner-function g in the paper."""
        # x: [T, B, memory_size]
        # memory: [1, B, memory_size, context_size]
        T = x.shape[0]
        B = x.shape[1]
        timestep = jnp.ones((T + 1, B), dtype=jnp.int32).reshape(T + 1, B, 1, 1)
        # Add context dim
        start = start.reshape(T, B, 1, 1)

        # Now insert previous recurrent state
        x = jnp.concatenate([state, x], axis=0)
        start = jnp.concatenate([jnp.zeros_like(start[:1]), start], axis=0)

        # This is not executed during inference -- method will just return x if size is 1
        _, new_state, _ = jax.lax.associative_scan(
            self.wrapped_associative_update,
            (start, x, timestep),
            axis=0,
        )
        return new_state[1:]

    @nn.compact
    def __call__(self, state: RecurrentState, inputs: Inputs) -> Tuple[RecurrentState, chex.Array]:

        # Add a sequence dimension to the recurrent state.
        state = jnp.expand_dims(state, 0)

        # Unpack inputs
        x, start = inputs

        # Map the input embedding to the recurrent state space.
        # This maps to the format required for the associative scan.
        scan_input = self.map_to_h(x)

        # Update the recurrent state
        state = self.scan(scan_input, state, start)

        # Map the recurrent state back to the output space
        out = self.map_from_h(state, x)

        # Take the final state of the sequence.
        final_state = state[-1:]

        # Remove the sequence dimemnsion from the final state.
        final_state = jnp.squeeze(final_state, 0)

        return final_state, out

    @nn.nowrap
    def initialize_carry(self, batch_size: int) -> RecurrentState:
        return jnp.zeros((batch_size, self.trace_size, self.context_size), dtype=jnp.complex64)
