"""The agent roster: model configs (with the VRAM and speed measurements
behind each) and the agents that serve them.

Read the roster through ``Config`` (config.py), which binds every name
here: Config is the interface callers read and tests patch.
"""
import os
from pathlib import Path

from .agent_prompts import (
    _ARCHITECT_SYSTEM_PROMPT,
    _IMPLEMENTER_SYSTEM_PROMPT,
    _REVIEWER_SYSTEM_PROMPT,
    _SYNTHESIZER_SYSTEM_PROMPT,
    _UNICODE_GUARD,
)


# llama-server v0.7.0 (upstream 620d222, no local patch: #29941 and #29953
# carry the MUL_MAT_ID padding fix that DEV-852 patched locally on v0.4.1 and
# v0.5.0) serves every config below. Each was measured on v0.5.0 and v0.7.0
# back to back on 2026-10-21 (DEV-933), on the argv LlamaServerManager builds,
# with the desktop holding 1,108 MiB (the free-MiB figures in the per-config
# notes were taken on other builds and other desktop sizes: read them as
# deltas, not absolutes). Decode tok/s, short (116-token) prompt / ~22K-token
# prompt, then MiB free after load:
#   implementer      81.6 / 74.6    649    (v0.5.0: 80.2 / 74.5   717)  n_cpu_moe 20; 22 since DEV-940
#   ornith           72.7 / 69.5   1307    (v0.5.0: 69.1 / 66.2  1343)
#   deep_implementer 25.9 / 25.9   5512    (v0.5.0: 25.8 / 25.3  5172)
#   fast_implementer 63.3 / 48.3   1311    (v0.5.0: 59.2 / 46.4  1311)
#   122B (retired)   20.6 / 19.6   2554    (v0.5.0: 19.8 / 18.9  2422)
#   deep_reviewer    24.4 /  --    4674    (v0.5.0: 21.6 / 21.7  3582)
#   moe_implementer  12.0 / 10.3   1304    (v0.5.0: 11.1 /  9.7  1400)
#   dense_architect  24.3 / 17.9    432    (v0.5.0: 23.6 / 17.9   460)
#   qwen38_architect 20.5 / 16.0   1202    (v0.5.0: 20.0 / 15.5  1270)
#   glimmer          13.8 / 11.8   1259    (v0.5.0: 13.4 / 11.5  1314)
# Decode is level or up to 8% faster and prefill up 2-15% on the GPU-resident
# configs. Memory moved both ways: the cpu_moe rungs with experts in host RAM
# got lighter (deep_implementer 340 MiB, the 122B 132, flashnext 1,092)
# and the GPU-resident configs 28-96 MiB heavier. dense_architect has the
# least headroom. Greedy output differs from v0.5.0 on seven configs and
# matches on three (fast_implementer, moe_implementer, glimmer). flashnext's
# long-prompt decode on v0.7.0 returned no tokens on the raw /completion
# bench (a 22K-token source file as the prompt), a bench-without-template
# artefact: through the chat path it matched the 122B's wall at depth
# (DEV-934). The 122B row is Qwen3.5-122B, retired by DEV-935; flashnext
# serves deep_reviewer and synthesizer since then.


# Root for model weights. Every model config also has its own MODEL_PATH_*
# env override; this only de-personalizes the defaults (DEV-199) — derived
# from the running user's home instead of a hardcoded username, so it
# resolves identically on the deploy box.
_MODELS_ROOT = os.getenv(
    "CODING_MODEL_MODELS_ROOT", str(Path.home() / ".lmstudio" / "models")
)


# ============================================================================

def _create_model_config(path_env, path_default, n_gpu_layers, n_ctx=32768, n_batch=2048,
                         server_extra_args=None, logit_bias=None, type_k=8, type_v=8,
                         repeat_penalty=1.15, repeat_last_n=256, cpu_moe=False,
                         n_cpu_moe=None, n_cpu_ffn=None, n_ubatch=512, draft=None,
                         escape_in_prompt=None):
    """Helper function to create standardized model configurations.

    Args:
        type_k: GGML type for KV cache keys (8=Q8_0, 2=Q4_0). Default Q8_0.
        type_v: GGML type for KV cache values (8=Q8_0, 2=Q4_0). Default Q8_0.
        repeat_penalty: Penalizes repeated tokens (1.0=off). Lower values help code generation.
        repeat_last_n: Window of recent tokens to apply repeat penalty to (256=windowed, -1=full context).
        cpu_moe: Keep ALL MoE expert weights on CPU. Allows more attention layers on GPU.
        n_cpu_moe: Keep only the first N layers' MoE experts on CPU, rest on GPU
            (llama-server --n-cpu-moe). Overrides cpu_moe when set. Lower N => more
            experts on GPU => faster decode, bounded by VRAM (KV competes for it).
        n_cpu_ffn: Keep only the first N layers' DENSE FFN weights on CPU
            (llama-server --n-cpu-ffn, 0.4.x and later). The dense counterpart
            to n_cpu_moe, but NOT the same trade: MoE experts are sparse, so
            offloading them streams a fraction of what it frees, while dense
            FFN is 100% active every token. What it buys is that attention and
            the KV cache stay on the GPU, so the CPU's share of the work stops
            growing with prompt depth. Set n_gpu_layers to cover every block
            when using it, or the two offloads compound (DEV-742).
        n_ubatch: Physical micro-batch size for prompt processing (default 512).
        draft: Optional speculative-decode draft. Dict with keys:
            path (str, required) — same-tokenizer model file
            n_gpu_layers (int, default 0)
            n_ctx (int, default same as target)
            cpu_moe (bool, default False)
            draft_max (int, default 4)
            draft_min (int, default 1)
            draft_p_min (float, default 0.75)
    """
    config = {
        'path': os.getenv(path_env, path_default),
        'n_gpu_layers': n_gpu_layers,
        'n_ctx': n_ctx,
        'n_batch': n_batch,
        'n_ubatch': n_ubatch,
        'type_k': type_k, 'type_v': type_v,
        'repeat_penalty': repeat_penalty,
        'repeat_last_n': repeat_last_n,
        'cpu_moe': cpu_moe,
        'n_cpu_moe': n_cpu_moe,
        'n_cpu_ffn': n_cpu_ffn,
    }
    if server_extra_args is not None:
        config['server_extra_args'] = server_extra_args
    if logit_bias is not None:
        config['logit_bias'] = logit_bias
    if escape_in_prompt:
        # {literal: replacement} applied to every message's text before the
        # request leaves the server (DEV-934).
        config['escape_in_prompt'] = dict(escape_in_prompt)
    if draft is not None:
        config['draft'] = draft
    return config



def _create_agent_config(description, system_prompt, model_config, executor=False,
                         system_prompt_native_tools=None,
                         chat_template_kwargs=None):
    """Helper function to create standardized agent configurations.

    ``system_prompt_native_tools`` is an optional alternative system prompt that
    the chat handler swaps in when the request carries an OpenAI ``tools``
    array. Use it to drop marker-format guidance for tools that have been
    migrated to native function-calls (otherwise the marker docs collide with
    the schema and the model emits malformed hybrids).

    ``chat_template_kwargs`` is forwarded to llama-server's Jinja renderer, so
    a hybrid template's conditionals can be set per AGENT rather than per call
    site — most importantly ``{'enable_thinking': False}`` (DEV-556). Two agents
    may then share one GGUF and differ only in whether the template opens a
    reasoning block, which is what makes the thinking-on/off head-to-head a
    plain roster comparison with no model swap.

    Omitted by default: absent the key the payload is byte-identical to what
    the server sent before this existed, so every agent keeps the template's
    own default until one deliberately opts out. Requires ``--jinja`` on the
    model (llama-server ignores it otherwise).
    """
    config = {
        'description': description,
        'system_prompt': system_prompt,
        'model_config': model_config,
    }
    if executor:
        config['executor'] = True
    if system_prompt_native_tools is not None:
        config['system_prompt_native_tools'] = system_prompt_native_tools
    if chat_template_kwargs:
        config['chat_template_kwargs'] = dict(chat_template_kwargs)
    return config


# ============================================================================
# Configuration
# ============================================================================


# ── Shared model configs ──
# FAST: Lightweight Q4_K_M for quick implementation tasks.
# Migrated 2026-04-30 from llama_cpp (ngl=26, 262K Q4_0 KV) to llama_server
# + cpu_moe. Headroom from cpu_moe redirected to KV-quant upgrade per
# feedback_kv_quant_preference: traded 262K Q4_0 → 196K Q8_0.
#
# 2026-05-04: ub reduced 4096 → 3584. At ub=4096 llama-server projected
# 15,312 MiB needed / 14,933 free post-dense_architect-swap → cudaMalloc
# OOM, ~400 MiB short. ub=3584 saves ~819 MiB compute buffer (1.6 MiB/
# ub × 512), giving ~400 MiB safety margin against fragmentation. Cost:
# ~12% prefill batch reduction; on a "fast" implementer that already
# rotates first in the chain, prefill speed isn't the binding factor —
# OOM-free swap is.
# Expert-offload tuned 2026-06-03: --cpu-moe (all experts on CPU) was decode-
# bound on the AVX2 CPU. Trading 192K->64K context frees VRAM to push 24 of 48
# expert layers onto the RTX 5080 via n_cpu_moe=26 — measured +59% decode
# (37 -> ~59 tok/s) at 64K with ~1.5 GB VRAM headroom (-ncmoe 24 = +70% but
# only 0.85 GB free). See project_llama_server_build_perf.
_MOE_30B_FAST = _create_model_config(
    'MODEL_PATH_30B_FAST',
    f'{_MODELS_ROOT}/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf',
    49, 65536, 3584,
    server_extra_args=['--chat-template', 'chatml', '--swa-full'],
    logit_bias=[[151657, -100.0], [151658, -100.0]],
    cpu_moe=True, n_cpu_moe=26, n_ubatch=3584,
)

# NEXT: Qwen3-Coder-Next-Q8_0 (80B MoE with 3B active params)
# Very smart but runs mostly on system RAM (slow). Native 256k context enabled.
# ngl=48 (--cpu-moe): 8,304 MiB free. All 48 attention layers on GPU.
# v0.7.0 frees 340 MiB against v0.5.0 for this config (5,512 vs 5,172 free,
# 2026-10-21); v0.5.0 had needed 256 MiB more than v0.4.1.
# --swa-full enables prompt cache reuse (avoids full re-prefill each turn).
# n_batch/n_ubatch=4096 for faster prefill (8 GB headroom supports large batches).
_MOE_80B_Q8 = _create_model_config(
    'MODEL_PATH_80B_Q8',
    f'{_MODELS_ROOT}/unsloth/Qwen3-Coder-Next-GGUF/Q8_0/Qwen3-Coder-Next-Q8_0-00001-of-00003.gguf',
    48, 262144, 4096,
    server_extra_args=['--chat-template', 'chatml', '--swa-full'],
    logit_bias=[[151657, -100.0], [151658, -100.0]],
    cpu_moe=True, n_ubatch=4096,
)

# Retired 2026-07-14: _MOE_480B_LITE (the 480B at IQ1_M, ~1.7 bpw) backed a
# `lite_architect` agent that existed to be the *fast* architect. It wasn't.
# Measured A/B against ULTRA (median of 3, llama.cpp's own timings):
#   architect      Q2_K_XL  180.3 GB   6.38 tok/s decode   14,456 MiB VRAM
#   lite_architect IQ1_M    149.7 GB   6.80 tok/s decode   14,206 MiB VRAM
# Decode here is bandwidth-bound on 35B active experts crossing DDR5, so a
# 17% smaller model should have decoded ~20% faster. It managed 6.6% — the
# IQ1 kernel ate two thirds of the win (REPACK covers Q4_0/Q4_K/IQ4_NL, not
# IQ1_M). Paying a 2.7 -> 1.7 bpw quality cliff, on the one agent whose whole
# job is reasoning quality, to buy 0.42 tok/s that no human perceives.
# The GGUF is still on disk; restore this block and the agent entry if you
# ever want the 30 GB of RAM headroom back (180.3 GB leaves only ~8 GB free).
# The real lever is fewer ACTIVE params, not fewer bits — but NOT via the
# 397B-A17B this once pointed at: that model was already retired for quality
# (DEV-93), and the only local copy is IQ1_M anyway. The fewer-active-params
# model that actually won is dense_architect (Qwen3.6-27B); see DEV-93 and the
# note on _MOE_480B_ULTRA for where that lever really lives.

# Retired 2026-07-14 (DEV-99): _MOE_480B_ULTRA (Qwen3-Coder-480B-A35B Q2_K_XL,
# ~180 GB on disk, 35B active) backed the interactive `architect`. It LOST the
# quality eval this note (and DEV-93) always flagged as the open question:
# dense_architect (Qwen3.6-27B) beat it 4-2 (0 ties) on 6 architect-shaped
# design / decomposition / trade-off / failure-analysis tasks, blind and
# counterbalanced, judged by Claude via the Claude Agent SDK (DEV-98). Every
# verdict held under order-swap. The 480B's only 2 wins were the most
# multi-part prompts, and the judge scored those on COVERAGE, not reasoning —
# the 27B ran out of a fixed 1400-token eval budget while comparable on core
# correctness/insight. So the 480B bought slower, VRAM-hungrier completeness
# under a tight budget, not better design. `architect` now points at
# _DENSE_27B: ~1.7x decode (6.3 -> 10.8 tok/s, DEV-95), 1/10th the VRAM
# (168 -> 16.8 GB, ~150 GB RAM reclaimed), 4x the context (32K -> 128K).
#
# Do NOT re-point `architect` back at the 480B without a fresh eval that beats
# this one: both signals we have favor the 27B — this head-to-head, and
# SWE-bench Verified (Qwen3.6-27B 77.2 vs the retired 397B's 76.2, commit
# d2dd54a7). The GGUF is still on disk and scripts/download_models.py still
# lists it; to restore, re-add the agent entry and this config:
#   _MOE_480B_ULTRA = _create_model_config(
#       'MODEL_PATH_480B_ULTRA',
#       '.../Qwen3-Coder-480B-A35B-Instruct-UD-Q2_K_XL-00001-of-00004.gguf',
#       63, 32768, 4096,
#       server_extra_args=['--chat-template', 'chatml', '--swa-full'],
#       logit_bias=[[151657, -100.0], [151658, -100.0]],
#       cpu_moe=True, n_ubatch=4096)
# Restore caveats that cost time to learn:
#   * KV layout: native ctx 262144 is unreachable on a 16 GB GPU (8 KV heads x
#     62 layers x 64 head_dim => 33 GB Q8_0 at 256K); 32K was the largest ctx
#     where Q8_0 KV still fit (feedback_kv_quant_preference).
#   * Spec-decode does NOT help it: a Coder-30B-A3B Q4_K_M draft regressed
#     decode ~18% via CPU mem-BW contention (2026-05-03, DEV-96).
#   * The 397B-A17B is NOT a faster substitute (DEV-93): retired for quality,
#     and the only local copy is IQ1_M (no REPACK path, lands below 76.2).

# MINIMAX: MiniMax M2.5 (230B MoE, 10B active params, 62 layers, ~1,760 MiB/layer)
# Uses llama-server subprocess backend with native Jinja template
# ngl=4 at 32K Q8_0: 6,207 MiB free (measured 2026-03-30)
# ngl=6 at 65K Q4_0: testing (est. ~2,500 MiB free)
# ngl=6 (no --cpu-moe): 6,207 MiB free | ngl=6 (--cpu-moe): 12,665 MiB free
# 62 attention layers total. With --cpu-moe, targeting ngl=62 (all layers).
# KV at 65K Q4_0: 4,392 MiB (62 GPU layers). 7,188 MiB free at ngl=62.
# Bumping to 98K Q4_0: ~6,588 MiB KV → ~1 GB free. Tight but fits.
# Q5_0 cache OOM at ngl=62 (10 GB compute buffer). Staying at Q4_0.
# MiniMax has less headroom (4.8 GB) — use 2048 ubatch (conservative)
_MOE_230B = _create_model_config(
    'MODEL_PATH_230B',
    f'{_MODELS_ROOT}/unsloth/MiniMax-M2.5-GGUF/Q4_K_M/MiniMax-M2.5-Q4_K_M-00001-of-00004.gguf',
    62, 118784, 4096, n_ubatch=4096,
    server_extra_args=['--jinja', '--reasoning-format', 'none', '--swa-full'],
    logit_bias=[[200052, -100.0], [200053, -100.0]],
    type_k=2, type_v=2,
    cpu_moe=True,
)

# ── Qwen3.8-Flash-Next: the reviewer rung (deep_reviewer, synthesizer) ──

# QWEN3.8-FLASH-NEXT UD-Q4_K_XL — the reviewer rung since DEV-935 (2026-10-21),
# replacing Qwen3.5-122B-A10B Q4_K_M, which it beat 4-1-1 (DEV-930) and
# 5-2-1 of 8 with every task judged (DEV-934) on the blind reviewer eval, at
# about twice the tokens and 1.6x the wall per review; Keith took quality
# over cost. 125B MoE (512 experts, 10 used, 640-wide) plus a 51B n-gram
# embedding table, 6B active per token; arch qwen4exp, 48 blocks,
# full_attention_interval=4 so 12 blocks keep a KV cache (2 KV heads) and 36
# hold DeltaNet state; 262144 native context. Served by llama-server v0.7.0,
# which carries the upstream qwen4exp fix (#29751) and its MTP (#29761).
# Licence is qwen-community-1.0, not Apache; Keith accepted it 2026-10-21 for
# this non-commercial use.
#
# Rung: all blocks on the GPU with the experts on the CPU, at a 128K window
# and Q8_0 KV (12 KV blocks of 2 heads, so the window is cheap). Measured
# 2026-10-21 on v0.7.0: 11,166 MiB used, 4,674 free after a short generation
# (v0.5.0: 12,267 used, 3,572 free), 111 GB mmapped from page cache in under
# a minute, 24.4 tok/s on the short prompt against the 122B's 20.6, and the
# same wall at depth through the chat path (76 s vs 72 s on a 25K-token
# prompt). The native 256K window is affordable here if a prompt ever needs
# it. MTP: the repo ships separate draft files (MTP/*.gguf); not wired until
# draft-mtp for qwen4exp is confirmed on this build, so that speed is a floor.
#
# Known fault, DEV-930 (cause found 2026-10-21; mitigations DEV-934): with
# thinking on, the model emits EOS inside its <think> block when it is about
# to write a think-tag token as quoted text, so only prompts that mention
# <think> or </think> trigger it (this repo's own source does). The Qwen
# tokenizer makes each tag one USER_DEFINED token whatever parse_special
# says; v0.5.0 and v0.7.0 each failed 6 of 15 diagnostic calls in different
# cells, de-fanging the input rescued 3 of 4, and thinking off never failed.
# Both mitigations serve: escape_in_prompt below rewrites the two tags in
# every prompt, and the server retries a reasoning-only completion once with
# enable_thinking off (llama_server.proxy_sync). The DEV-934 eval ran 8 of 8
# with neither a 502 nor a fallback.
_MOE_FLASHNEXT = _create_model_config(
    'MODEL_PATH_FLASHNEXT',
    f'{_MODELS_ROOT}/unsloth/Qwen3.8-Flash-Next-GGUF/UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf',
    49, 131072, 2048,
    server_extra_args=['--jinja', '--reasoning-format', 'none', '--swa-full'],
    cpu_moe=True, n_ubatch=2048,
    escape_in_prompt={'<think>': '< think >', '</think>': '< /think >'},
)

# ── Qwen3.6 family (replaces Qwen3.5-35B implementer + 122B/397B architects) ──

# Qwen3.6-35B-A3B UD-Q4_K_M — direct successor to Qwen3.5-35B-A3B (same
# MoE shape: 3B active, 35B total). Unsloth Dynamic 2.0 quant. 22.1 GB.
# Released 2026-04-16. cpu_moe puts experts on CPU, attention on GPU.
# Measurements (2026-04-24):
#   ngl=48 ctx=131K Q4_0 ub=2048 → 6,026 MiB used, 9,784 free (initial)
#   ngl=48 ctx=262K Q8_0 ub=4096 → 12,585 MiB used, 3,225 free (1st tune)
#   ngl=48 ctx=262K Q8_0 ub=5120 → 14,260 MiB used, 1,550 free (2nd tune)
#   ngl=48 ctx=262K Q8_0 ub=6144 → OOM at compute buffer alloc (CUDA -6)
#   ngl=48 ctx=262K Q8_0 ub=5632 → estimated 15,094 used / 716 free (3rd)
#   ngl=48 ctx=262K Q8_0 ub=4608 → estimated 14,427 used / 1,376 free
# Compute buffer scales 1.63 MiB/ub-unit observed 4096→5120. Pushing
# beyond ub=5632 exhausts the headroom margin; ub=6144 confirmed OOM.
#
# 2026-05-04: ub reduced 5632 → 4608. The 716 MiB free at ub=5632 sat
# below the 1,200 MiB safety floor and risked transient OOM during
# model-swap (observed in spec_fa78ca9c retry-4 deep_reviewer→implementer
# swap). ub=4608 buys ~1,670 MiB compute-buffer savings → ~1.4 GiB free,
# finally above the safety floor. Trade: ~18% prefill batch reduction.
# Expert-offload tuned 2026-06-03: 262K->64K ctx + n_cpu_moe=26 (22 of 48
# expert layers on the RTX 5080): measured +30% decode (51->66 tok/s) at 64K,
# ~1.7 GB VRAM free. See project_llama_server_build_perf.
# Re-tuned 2026-06-05 after llama-server upgrade d132f22->5343f45 (CUDA 12.8):
# the new binary is ~2.7 GB more VRAM-efficient, so n_cpu_moe=26 measured 4.4 GB
# free on 5343f45. Swept n_cpu_moe on the new binary (decode tok/s @ peak free):
#   26->68.7@4431 | 22->76.3@2575 | 20->77.8@1646 | 19->81.0@1182 | 18->83.4@719 | <=14 OOM.
# n_cpu_moe=22 is the knee: +11% decode vs 26 with 2.5 GB free (well above the
# ~1.4 GB swap floor). 21/20 add ~0 decode; 19/18 add +18/+21% but drop under the
# swap floor and risk the spec_fa78ca9c-style swap OOM.
# WAS 18 (user override 2026-06-05), on the reading that it bought +21% decode.
#
# CHANGED TO 20 (2026-07-13). Two things forced it:
#
# 1. At 18 the implementer left only ~496-570 MiB free — under _VRAM_MARGIN_MIB
#    (500), so _check_vram_or_raise refused every RELOAD. The first load in a
#    process is exempt (nothing recorded yet) and records the footprint; every
#    load after that 503'd. Since the watchdog reaps the child after IDLE_TIMEOUT,
#    the server bricked itself after ~30 min idle until restarted by hand. This
#    was not the "occasional swap-OOM retry" the note above anticipated.
# 2. The +21% was an artifact. Decode drifts up ~15% over a session (65->75 tok/s),
#    and sweeps walk N descending, so N=18 was always measured LAST, at peak warmth.
#    Order-balanced paired runs put 18-vs-20 at ~2% decode, not the 6.7% the
#    2026-06-05 sweep reported. scripts/sweep_cpu_moe.py now warms up and takes a
#    median over --reps to stop this recurring.
#
# 20 costs ~2% decode and leaves ~1500 MiB free — clears the VRAM guard AND the
# ~1.4 GB swap floor. See [[project_llama_server_child_lifecycle]].
#
# ngl 48 -> 41 (2026-07-14). The model has 40 blocks, not 48, so llama.cpp was
# silently clamping: no behaviour change, but the config, its description, and
# the README all advertised a layer count this model does not have. 41 is the
# honest spelling of "all 40 blocks + the output layer" — llama.cpp counts
# output as the 41st, the same convention as architect's ngl=63 over 62 blocks.
# NOT 40: that would leave the output layer on CPU, and it runs every token.
#
# Re-swept 2026-07-14 after DEV-94 removed the reload cliff (median of 3, warm-up
# discarded, production argv). STAYS AT 20 — the guard fix did not buy this model
# anything:
#   n_cpu_moe=24 -> 73.52 tok/s @ 3,696 MiB free
#             22 -> 77.54          @ 2,770
#             20 -> 80.73          @ 1,842   <- stays
#             18 -> 84.75          @   914
#             16 -> fails to load (SIGABRT) — the hard ceiling
# 18 is +5% decode but leaves 914 MiB, under the ~1,400 MiB swap floor, and that
# floor is not what DEV-94 fixed: it exists because of a real swap-time OOM
# (spec_fa78ca9c), which is transient contention during teardown->start, not the
# reload arithmetic. So 20 remains the honest pick for THIS model.
#
# Correction to the 2026-07-13 note above, for anyone reading it as evidence:
# at 18 this model measures 914 MiB free, not the 496-570 recorded there. On
# today's numbers the old guard would NOT have refused the reload, so that
# note's account of the brick does not reproduce as written. The brick was
# real (see DEV-94); the free-VRAM figure attached to it was not reliable.
#
# CHANGED TO 22 (2026-10-22, DEV-940). The 1,842 MiB free of the 07-14 sweep
# was gone: on llama-server v0.7.0 with the desktop holding ~1,170 MiB
# (Sunshine, a Chrome GPU process, the compositor) the rung at 20 measured
# 497 and 584 MiB free after load on two spawns, under or at the 500 MiB
# _VRAM_MARGIN_MIB that admits a RELOAD, which is the DEV-742 brick one
# desktop process away. Levers measured standalone the same night, same
# session, production argv, 64K window, free MiB after load / decode tok/s
# short / ~22K / prefill tok/s long:
#   ub 4608, moe 20 (was)   584   76.9 / 72.5 / 3,520
#   ub 4096, moe 20         736   78.2 / 72.2 / 3,101   (ubatch buys 152, not
#                                                        the ~830 the 05-04 note
#                                                        extrapolated; -12% prefill)
#   ub 4608, moe 21       1,048   75.5 / 70.7 / 3,437
#   ub 4608, moe 22       1,512   73.6 / 68.6 / 3,389   <- now
#   ub 4096, moe 21       1,200   75.2 / 70.8 / 3,156
# 22 is the one lever that clears the ~1,400 MiB swap floor on its own, at
# about 5% decode. 21 would do for the reload margin alone; the floor is the
# swap-time OOM (spec_fa78ca9c), so it wins. A larger window (DEV-937, 96K)
# was measured the same night and declined: 812 free at Q8_0 KV at this
# setting, and the 64K agents fit two thirds of implementer prompts as is.
_MOE_35B = _create_model_config(
    'MODEL_PATH_35B',
    f'{_MODELS_ROOT}/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf',
    41, 65536, 4608,
    server_extra_args=['--jinja', '--reasoning-format', 'none', '--swa-full'],
    type_k=8, type_v=8,
    cpu_moe=True, n_cpu_moe=22, n_ubatch=4608,
    repeat_penalty=1.05,
)

# ORNITH-1.5-35B-A3B Q4_K_M (DEV-929) — eval-only challenger to `implementer`.
# Official GGUF from ornith-ai (MIT), arch qwen35moe: the same 3B/35B shape
# as _MOE_35B (40 blocks, 256 experts / 8 active), so it takes the incumbent's
# rung unchanged and the model is the only variable in the A/B. Ornith-1.0 was
# retired on the DEV-90 eval (a wash, edge to implementer; note above); 1.5
# claims a far wider gap and gets the same blind, Claude-judged test. If the
# load lands under the ~1.4 GB reload floor on v0.5.0, re-sweep n_cpu_moe
# before judging anything.
_MOE_ORNITH = _create_model_config(
    'MODEL_PATH_ORNITH',
    f'{_MODELS_ROOT}/ornith-ai/Ornith-1.5-35B-A3B-GGUF/Ornith-1.5-35B-Q4_K_M.gguf',
    41, 65536, 4608,
    # --reasoning-budget (DEV-931): the 1.5's template has no reasoning_effort,
    # only enable_thinking, and on DEV-929's moe_offload task it reasoned to the
    # 8,000-token cap four times running at temp 0 with an identical tail, so
    # the request was a 502 (DEV-617). The forced </think> at 4096 is DEV-616's
    # treatment of the 3.8; the daemon's allocator reserves the figure.
    server_extra_args=['--jinja', '--reasoning-format', 'none', '--swa-full',
                       '--reasoning-budget', '4096'],
    type_k=8, type_v=8,
    cpu_moe=True, n_cpu_moe=20, n_ubatch=4608,
    repeat_penalty=1.05,
)

# Ornith-1.5 is back ON EVAL as _MOE_ORNITH below (DEV-929). The 1.0 record:
# Retired 2026-07-14: _MOE_ORNITH (Ornith-1.0-35B Q4_K_M, DeepReinforce, MIT)
# backed an `ornith` agent added ON EVAL in DEV-88 to test the lab's self-
# reported SWE-bench 75.6 / Terminal-Bench 64.2 claims against `implementer`.
# It was a Qwen3.5 fine-tune of the same 3B/35B shape (40 blocks, 16 heads, 2
# KV, 256 experts / 8 active), architecturally IDENTICAL to _MOE_35B.
#
# Speed was a wash (DEV-95): 80.9 vs implementer's 75.5 tok/s decode was pure
# file-size (0.9 GB smaller), no architectural edge. Quality was the only open
# question, and DEV-90 answered it with the blind, counterbalanced eval:
#   Gemini (external judge, 5/8 tasks before its free tier rate-capped): 5 ties.
#   deep_reviewer (local Qwen3.5 judge — if biased, biased TOWARD ornith):
#     implementer 2, ornith 1, tie 5.
# No task where both judges agreed ornith won. An ornith-friendly judge still
# favored implementer 2-1 on the decisive tasks. Verdict: a wash, edge to
# implementer. A model that does not beat the incumbent is not worth a roster
# slot or 21.2 GB — the same call as lite_architect. See DEV-90.
#
# The GGUF is still on disk and scripts/download_models.py still lists it.
# To re-eval (e.g. once DEV-98's Claude judge lands), restore this block plus
# the `ornith` agent entry from this commit; the offload sweep that picked
# n_cpu_moe=18 (86.80 tok/s @ 1,544 MiB free) is in the DEV-90 removal commit.

# Qwen3.6-27B Q4_K_M — DENSE 27B model, ~16.8 GB. Released 2026-04-22.
# 65 blocks (GGUF qwen35.block_count), hybrid: full_attention_interval=4, so
# only ~16 of them keep a growing KV cache and the other 49 hold constant SSM
# state. Dense (no cpu_moe possible). 16 GB VRAM forces partial GPU offload.
# Pushed ngl 20→36→40 across two iterations.
# Measured 2026-04-24 at ngl=36: 13,513 MiB used, 2,297 free.
#
# MTP wired 2026-06-05: model -> unsloth/Qwen3.6-27B-MTP-GGUF (Q4_K_M with the
# native multi-token-prediction head embedded, +0.29 GB) + `--spec-type
# draft-mtp --spec-draft-n-max 2`. The dense 27B decodes slowly (29 of its 65
# layers ran on CPU at the old ngl=36); MTP ~doubles it. Measured on build 5343f45 with the prod
# global flags (lookup-cache + cache-reuse, no conflict): baseline 8.3 tok/s
# -> MTP 14.1 tok/s decode = ~1.7x, ~85% draft acceptance on structured output.
# ngl=36 (was 40->38->36): 1,754 free / 13.5 tok/s decode. ngl=40 OOM-tight
# (714 free); ngl=38 (1,287 free) CRASHED the autonomous architect-revision
# pass — design-review (#3) swaps back to q36 AND the larger r1 prompt
# (design + review feedback ~4.6K tok) OOM'd the prefill compute buffer
# (SIGABRT rc=-6; spec_b956e1c9, 2026-06-13 — see [[project_model_swap_oom]]).
# 36 restores the headroom that pass needs (~−0.6 tok/s decode is worth it).
#
# DEV-707, 2026-09-16 — ngl 36→46 and n_ctx 131072→65536, Keith's call. The
# window is the ONLY thing tradeable for layers on a 16 GB card whose weights
# are 15.93 GiB, and halving it buys ten. Measured on the live box, same
# 400-token architect-style prose generation each time:
#   ngl=36 @ 131072   13.69 tok/s decode @ 1,488 MiB free   (the old default)
#   ngl=41 @ 131072   16.24                @   191 MiB free
#   ngl=46 @  65536   18.72                @   884 MiB free   <- this config
#   ngl=53 @  32768   25.27                @   554 MiB free (b/ub 1024)
# At realistic depth (a 52,818-token prompt) it is 8.27 → 11.33 tok/s, and the
# prefill compute buffer survived that prompt with 684 MiB still free — the
# case the ngl=38 note above did NOT have. Two-point fit: ~259 MiB per layer,
# ~31 KiB per context token (so the 131K KV was ~4.0 GB).
# The ngl=38 crash above happened at 1,287 MiB free, which is MORE headroom
# than this config has; the difference is that its 2 GB of reclaimed KV is not
# available to the prefill buffer. If an architect revision pass SIGABRTs,
# back off to ngl=44 (~1.4 GB free) before blaming anything else.
# Note `--swa-full` is a no-op here: llama-server logs "swa_full is not
# supported by this model, it will be disabled" on every load. Left in place
# so this change is only the two numbers.
# Quality lossless (verified tokens == base model). Used by dense_architect.
# See [[project_mtp_test_scope]] / [[project_llama_server_upgrade]].
# DEV-744, 2026-09-19 — ngl 46 -> 66 with n_cpu_ffn=33, on llama-server
# v0.4.1. This SUPERSEDES the DEV-707 note above, whose premise ("the window
# is the ONLY thing tradeable for layers") was true only of --n-gpu-layers.
#
# ngl=46 put 19 of 65 blocks entirely on the CPU: their FFN, their attention
# weights, their KV cache AND their attention compute. Attention cost scales
# with context; FFN cost does not. So the old rung paid more the deeper the
# prompt went, and --n-cpu-ffn moves ONLY the FFN, leaving attention and KV
# resident. ngl=66 covers every block (65) plus output, so -ncffn carries the
# whole offload by itself — do not lower ngl here expecting more headroom,
# the two offloads would compound and 24/27 both OOM'd in the sweep.
#
# Measured (DEV-742, standalone, same box, same argv, one run per arm):
#     depth        ngl=46      ncffn=33
#      28 tok      18.30       21.58
#     ~26K         15.06       22.33   <- production band, +48%
#     ~51K         10.70       21.93
#   free MiB          851         951   (production's old rung: 884)
# The old rung falls 42% across that range; this one is flat. A repeat
# reproduced deep decode to 0.14% with byte-identical MTP draft counts
# (temp 0 through the MTP path is deterministic), and adding
# --slot-save-path changed nothing, so the win survives production's argv.
#
# 131072 was ALSO affordable here: ncffn=52 holds the full window on 1,404
# MiB free at 15.96 t/s deep, against 8.03 for the old ngl=36/131072 rung.
# Keith chose speed over window 2026-09-19 because architect prompts are
# median 8,518 tokens and have not exceeded 23,826 since 09-16 — the window
# is not the current constraint. Revisit if prompt sizes grow; it is a
# two-value change, not a rebuild.
_DENSE_27B = _create_model_config(
    'MODEL_PATH_27B',
    f'{_MODELS_ROOT}/unsloth/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-Q4_K_M.gguf',
    66, 65536, 2048,
    n_cpu_ffn=33,
    server_extra_args=['--jinja', '--reasoning-format', 'none', '--swa-full',
                       '--spec-type', 'draft-mtp', '--spec-draft-n-max', '2'],
    type_k=2, type_v=2,
    n_ubatch=2048,
)

# QWEN3.8-27B UD-Q4_K_M (DEV-928) — the production architect since
# 2026-10-21, the planner stays _DENSE_27B (DEV-735). Same qwen35 hybrid arch and block count as _DENSE_27B, MTP
# head embedded in the GGUF (nextn_predict_layers=1), so the draft-mtp wiring
# is identical. Three earlier challenges (DEV-615, DEV-702) ended 3-3 / 5-1 /
# 3-3 against dense_architect and DEV-839 retired the agent on telemetry;
# the fourth, at reasoning_effort=medium, won 4-2 with no ties at cost
# parity (DEV-928), and Keith moved the design phase onto it.
#
# What this config changes: reasoning_effort=medium through the chat template.
# Every prior arm ran the template default (xhigh), and DEV-616 recorded what
# that buys on a 27B: unbounded rumination, then tool-call stubs once
# --reasoning-budget bites. The budget stays so a failure is visible rather
# than an empty answer.
#
# Rung: the incumbent's DEV-744 shape (ngl=66 covers all 65 blocks plus
# output, --n-cpu-ffn 33 carries the whole offload), not DEV-740's ngl=46.
# On llama-server v0.5.0 the ngl=46 rung loaded at 463 MiB free (2026-10-20
# probe), under the ~1.4 GB reload floor and under the 714 MiB at which the
# 3.6 SIGABRT'd in production; v0.4.x had measured it at 843. Same block
# count and a smaller file than the 3.6, so the 3.6's rung is the obvious
# match. Measured on v0.5.0, 2026-10-20, 65536 ctx Q4_0 KV, after a short
# probe generation:
#   ngl=46 (DEV-740 rung)       463 MiB free
#   ngl=66, n_cpu_ffn=33        555 MiB free   (the 3.6 reads ~950 here)
#   ngl=66, n_cpu_ffn=38      1,273 MiB free   <- this config
#   (v0.7.0, 2026-10-21, desktop 1,108: 1,202 free, 68 MiB heavier)
# The 3.8's footprint is ~400 MiB heavier than the 3.6's at the same rung,
# so it pays five more blocks of FFN on the CPU for the reload floor.
_DENSE_27B_38 = _create_model_config(
    'MODEL_PATH_27B_38',
    f'{_MODELS_ROOT}/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-Q4_K_M.gguf',
    66, 65536, 2048,
    n_cpu_ffn=38,
    server_extra_args=['--jinja', '--reasoning-format', 'none', '--swa-full',
                       '--spec-type', 'draft-mtp', '--spec-draft-n-max', '2',
                       '--reasoning-budget', '4096'],
    type_k=2, type_v=2,
    n_ubatch=2048,
)

# MUSE-GLIMMER-30B UD-Q4_K_XL (DEV-692) — Meta's open-weight distill of the
# HOSTED Muse Spark 1.1. Spark itself is not wirable here: every agent in this
# server is a local llama-server GGUF and there is no remote-provider path.
# Glimmer is the thing that exists on disk, and it is what "bring Spark
# online" resolves to (Keith confirmed 2026-09-15).
#
# general.architecture = `muse-glimmer`, a NEW arch — not a Qwen re-label.
# The PINNED build already speaks it: libllama.so.0.1.0 (a94d563, Aug 13)
# exports llama_model_muse_glimmer::load_arch_hparams / load_arch_tensors /
# graph, so no llama-server upgrade is needed. 52 blocks, native 131072 ctx,
# embedding_length 6656, head_count 32 / head_count_kv 2 (GQA 16:1),
# rope.freq_base 500000, sliding_window 2048 / pattern 4,
# final_logit_softcapping 20.0. The chat template is MULTIMODAL (it emits
# `<|patch|>` for image parts); we only ever send text parts, so that path
# stays cold, but do not assume the template is text-only if it is edited.
#
# SWEEP 2026-09-15 on the RTX 5080 (16,303 MiB), production argv (-fa auto,
# -lm mmap, --cache-reuse 256, --lookup-cache-dynamic, -np 1), 131072 ctx,
# Q4_0 KV, one fixed prompt, 256 predicted (decode / prefill tok/s @ MiB free):
#   ngl=28 --swa-full    8.1 /  236 @ 4,128     ngl=28 no-swa   8.1 / 182 @ 5,329
#   ngl=32 --swa-full    9.3 /  276 @ 2,946     ngl=32 no-swa   9.3 / 208 @ 4,253
#   ngl=36 --swa-full   10.9 /  328 @ 1,763  <- the 131K pick, superseded
#   ngl=40 --swa-full   13.2 /  407 @   579     ngl=36 no-swa  10.9 / 243 @ 3,173
#   ngl=44 --swa-full   OOM, exit 1             ngl=40 no-swa  13.2 / 298 @ 2,093
#                                               ngl=44 no-swa  16.6 / 384 @ 1,015
# --swa-full costs a FLAT ~1.1 GB at every rung and buys ~35% faster prefill
# plus prompt-cache reuse; decode is completely unaffected by it. ngl=36
# --swa-full is the only rung that clears the ~1.4 GB reload floor (DEV-616
# rejected 692 MiB free; the 3.6 crashed production at 714, spec_b956e1c9)
# while keeping the cache reuse the retry loop's --cache-reuse 256 depends on.
# ngl=40 at 579 free and ngl=44 no-swa at 1,015 free are both under it.
#
# DEV-692 item 5, 2026-09-17: `--reasoning-format none` is WRONG for this
# model and was copied here from the Qwen agents without checking. The flag
# means "leaves thoughts unparsed in message.content". Qwen's thoughts are
# `<think>` tags that streaming.strip_thinking removes, so `none` is
# harmless there. Glimmer reasons in HARMONY CHANNELS delimited by
# `<|start|>assistant to=user<|message|>` / `<|eom|>` / `<|eot|>`, which
# strip_thinking has no pattern for — so its planning reached
# parse_architect_response intact. Its planning discusses the output format
# in prose ("Ensure DESIGN block starts with <<<DESIGN>>> then # Architecture
# etc."), so it CONTAINS the marker, and the parser anchored on that instead
# of the real design that follows. All 3 item-5 attempts parsed and all 3
# were unusable; the control (dense_architect, same prompt, same harness)
# was 3/3 clean, so this is Glimmer-specific and not a parser bug.
# `deepseek` routes thoughts to message.reasoning_content and leaves
# content clean.
#
# DEV-727, 2026-09-18: RE-SWEPT AT 65,536 AND REPITCHED TO ngl=40. Read the
# second half of this note before using the numbers: the sweep was motivated
# by a VRAM hypothesis that the sweep itself then DISPROVED, and the rung
# change is kept on its own merits, not as a fix.
#
# Glimmer was carrying DOUBLE the incumbent's KV for a window the pipeline
# never asks for. Architect prompts are median 8,518 tokens across 207 live
# calls, and DEV-633's fit check budgets against the DESTINATION window,
# which for the production architect has been 65,536 since DEV-707.
#
#   ngl=36 @ 65536   10.9 dec  @ 2,951 MiB free
#   ngl=40 @ 65536   13.2 dec  @ 1,884 MiB free   <- THE PICK
#   ngl=44 @ 65536   16.6 dec  @   780 MiB free   (under the floor)
#   ngl=48 @ 65536   load failed
#   ngl=52 @ 65536   load failed
#
# Selection rule, fixed in writing BEFORE the numbers existed (DEV-727): take
# the rung with the MOST headroom that still gains layers over 36 — headroom
# was the thing under investigation and speed the bonus, so this is
# deliberately not the fastest rung that loads. Against the superseded 131K
# pick it is +121 MiB free, +4 layers and +21% decode at the same time. The
# ten-layer figure DEV-707 got on the 27B did not transfer and was never
# assumed to — different arch (52 blocks, GQA 16:1, sliding_window 2048),
# different KV per token, hence a sweep rather than a config edit. Sweep argv
# is production argv (var/telemetry/sweep_glimmer_65k.sh).
#
# THE 502 IS NOT A VRAM FAULT, and this rung does not fix it. DEV-727
# hypothesised the 1-in-6 "upstream inference error" was the prefill compute
# buffer failing to allocate. It is not. The upstream error, in all five
# occurrences of the DEV-692 item-6 run and again here, is
#
#   common_chat_peg_parse: unparsed peg-native output:  to=<<<READ_FILE>>>...
#   500 "The model produced output that does not match the expected
#        peg-native format"
#
# which this proxy surfaces as a 502. Glimmer emits its TOOL CALLS in harmony
# recipient syntax — `<|start|>assistant to=<<<MARKER>>>` — and llama-server's
# native (peg) parser for the harmony template cannot parse a recipient that
# is one of our markers. It fires only when the model elects to call a tool,
# which is why it looked task-correlated. Round 0 of a tool loop emits bare
# markers and parses clean; from round 1, once a tool RESULT is in the
# conversation, it switches to the channel form and 500s. Reproduced
# deterministically on 2026-09-18 at this rung with 1,839 MiB free — MORE
# headroom than the config that first showed it — which is what retires the
# VRAM hypothesis. This is the third face of the harmony-format problem
# behind DEV-692 item 5, not a memory fault.
#
# `--no-jinja` is NOT the way out: llama-server refuses the model outright
# with "this custom template is not supported, try using --jinja" (tested,
# 2026-09-18). Remaining candidates are on DEV-727.
_MUSE_GLIMMER_30B = _create_model_config(
    'MODEL_PATH_MUSE_GLIMMER_30B',
    f'{_MODELS_ROOT}/unsloth/Muse-Glimmer-30B-GGUF/Muse-Glimmer-30B-UD-Q4_K_XL.gguf',
    40, 65536, 2048,
    server_extra_args=['--jinja', '--reasoning-format', 'deepseek', '--swa-full'],
    type_k=2, type_v=2,
    n_ubatch=2048,
)

# ── Agent definitions ──
# 'executor': True means few-shot + fallback extraction are enabled.
AGENTS = {
    'implementer': _create_agent_config(
        'Implementer — Qwen3.6-35B-A3B UD-Q4_K_M (3B/35B MoE, 64K ctx Q8_0, ngl=41 n_cpu_moe=22, default)',
        _IMPLEMENTER_SYSTEM_PROMPT,
        _MOE_35B,
        executor=True
    ),
    # Same prompt and executor wiring as `implementer`, deliberately, so the
    # model was the only variable in DEV-929's eval (Ornith-1.0 lost DEV-90's).
    # Retry-only (DEV-931): in _IMPLEMENTER_ROTATION after fast_implementer,
    # not in ALLOWED_IMPLEMENTER_AGENTS or TIER_TO_IMPLEMENTER; retry_policy
    # says why. Being in AGENTS arms the DEV-676 window fit check for it.
    'ornith': _create_agent_config(
        'Implementer — Ornith-1.5-35B-A3B Q4_K_M (3B/35B MoE, 64K ctx Q8_0, ngl=41 n_cpu_moe=20, --reasoning-budget 4096, retry-only since DEV-931)',
        _IMPLEMENTER_SYSTEM_PROMPT,
        _MOE_ORNITH,
        executor=True
    ),
    'deep_implementer': _create_agent_config(
        'Implementer — Coder-Next Q8_0 (3B/80B MoE, 256K ctx Q8_0, ngl=48 cpu_moe, deep reasoning)',
        _IMPLEMENTER_SYSTEM_PROMPT,
        _MOE_80B_Q8,
        executor=True
    ),
    'fast_implementer': _create_agent_config(
        'Implementer — Coder-30B Q4_K_M (3B/30B MoE, 64K ctx Q8_0, ngl=49 n_cpu_moe=26, fast)',
        _IMPLEMENTER_SYSTEM_PROMPT,
        _MOE_30B_FAST,
        executor=True
    ),
    # Reviewer rung (DEV-935). reasoning_effort=medium as in DEV-928 and the
    # DEV-930/934 eval arm: the 3.8 line overthinks at the template default.
    'deep_reviewer': _create_agent_config(
        'Reviewer — Qwen3.8-Flash-Next UD-Q4_K_XL (6B/125B+51B MoE, 128K ctx Q8_0, ngl=49 cpu_moe, reasoning_effort=medium, deep judgment)',
        _REVIEWER_SYSTEM_PROMPT,
        _MOE_FLASHNEXT,
        executor=True,
        chat_template_kwargs={'reasoning_effort': 'medium'},
    ),
    # Synthesis and its repair round (DEV-406) have their own entry so that a
    # reviewer repoint does not silently change the synthesizer, and usage
    # telemetry can tell the two roles apart (DEV-900). It shares
    # _MOE_FLASHNEXT with deep_reviewer, so both run in one llama-server
    # child and a switch between them is no swap. It moved with the reviewer
    # (DEV-935) on a synthesis-shaped parse check, not a replay eval;
    # DEV-902's replay eval still decides the slot on its own merits.
    'synthesizer': _create_agent_config(
        'Synthesizer — Qwen3.8-Flash-Next UD-Q4_K_XL (6B/125B+51B MoE, 128K ctx Q8_0, ngl=49 cpu_moe, reasoning_effort=medium, same model as deep_reviewer)',
        _SYNTHESIZER_SYSTEM_PROMPT,
        _MOE_FLASHNEXT,
        executor=True,
        chat_template_kwargs={'reasoning_effort': 'medium'},
    ),
    'moe_implementer': _create_agent_config(
        'Implementer — MiniMax M2.5 Q4_K_M (10B/230B MoE, 116K ctx Q4_0, ngl=62 cpu_moe)',
        _IMPLEMENTER_SYSTEM_PROMPT + _UNICODE_GUARD,
        _MOE_230B,
        executor=True
    ),
    # ── Qwen3.6 agents (replaced retired Qwen3.5 architect tier) ──
    'dense_architect': _create_agent_config(
        'Architect — Qwen3.6-27B MTP Q4_K_M (27B dense, 64K Q4_0 ctx, ngl=66 n_cpu_ffn=33 + MTP spec-decode, default planner; the design phase moved to qwen38_architect in DEV-928)',
        _ARCHITECT_SYSTEM_PROMPT,
        _DENSE_27B,
        executor=True
    ),
    # The design-phase architect (AUTONOMOUS_ARCHITECT_AGENT in production .env,
    # DEV-928). Same prompt as dense_architect: the model and its reasoning_effort
    # are the only differences, so the planner and the architect read one rulebook.
    'qwen38_architect': _create_agent_config(
        'Architect — Qwen3.8-27B UD-Q4_K_M (27B dense + MTP, 64K Q4_0 ctx, ngl=66 n_cpu_ffn=38, reasoning_effort=medium, the design-phase architect since DEV-928)',
        _ARCHITECT_SYSTEM_PROMPT,
        _DENSE_27B_38,
        executor=True,
        chat_template_kwargs={'reasoning_effort': 'medium'},
    ),
    # ── Muse-Glimmer (DEV-692) ──
    # Retry-only: in _IMPLEMENTER_ROTATION, but not in
    # ALLOWED_IMPLEMENTER_AGENTS or TIER_TO_IMPLEMENTER, so it never makes
    # attempt 0 (retry_policy says why). Being in AGENTS is what arms the
    # DEV-676 window fit check for it: _agent_ctx_limit reads n_ctx off the
    # agent's model config.
    'glimmer_implementer': _create_agent_config(
        'Implementer — Muse-Glimmer-30B UD-Q4_K_XL (Meta, 64K Q4_0 ctx, ngl=40 --swa-full, retry-only since DEV-692)',
        _IMPLEMENTER_SYSTEM_PROMPT,
        _MUSE_GLIMMER_30B,
        executor=True
    ),
}

# Names that resolve to an agent on lookup; not listed in /v1/models.
# scripts/agent_usage.py reports which are still requested (DEV-839), and an
# alias nothing requests is retired. Deliberately no `architect`: two agents
# play that role (dense_architect plans, qwen38_architect designs), so a bare
# `architect` would pick one of them silently.
AGENT_ALIASES: dict[str, str] = {}
