"""Server configuration: model configs, agent registry, system prompts.

Extracted from server.py for organization. Imports are kept narrow so
this module can be loaded without pulling in FastAPI/HTTP machinery.
"""
import os
from pathlib import Path
from typing import List

from coding_model_autonomous.supervisor import SYSTEM_PROMPT as _SUPERVISOR_SYSTEM_PROMPT

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
                         n_cpu_moe=None, n_ubatch=512, draft=None):
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
    }
    if server_extra_args is not None:
        config['server_extra_args'] = server_extra_args
    if logit_bias is not None:
        config['logit_bias'] = logit_bias
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

class Config:
    PORT = int(os.getenv('PORT', 5000))
    HOST = os.getenv('HOST', '127.0.0.1')
    ADMIN_API_KEY = os.getenv('ADMIN_API_KEY', '')
    INGEST_ALLOWED_DIR = os.getenv('INGEST_ALLOWED_DIR', '')
    # Per-file byte cap for ingestion. Documented since forever but read by
    # nothing (DEV-164), so an operator who set it got no protection: the
    # only limit was the 100MB base64 upload cap, and PDF ingest had none.
    # 0 disables the cap. Default 100MB matches the upload ceiling rather
    # than the old .env.example's 100000 (100KB), which would have started
    # rejecting PDFs that ingest fine today.
    INGEST_MAX_FILE_SIZE = int(os.getenv('INGEST_MAX_FILE_SIZE', 100 * 1024 * 1024))
    
    # llama-server thread split. 24 = physical core count (8 P-cores + 16
    # E-cores); hyperthreads hurt decode. Prefill (batch) benefits from
    # hyperthreads, use all 32 threads.
    DEFAULT_N_THREADS = int(os.getenv('MODEL_N_THREADS', 24))
    DEFAULT_N_THREADS_BATCH = int(os.getenv('MODEL_N_THREADS_BATCH', 32))

    # Upper bound on a single llama-server inference call. Must be >= the
    # longest autonomous role timeout (coding_model_autonomous/executor.py defaults
    # ARCHITECT/REVIEWER to 2700s) so that the inner request doesn't fail
    # before the outer orchestrator's deadline. Override via env if you need
    # patience for slower hardware or longer max_tokens.
    LLAMA_SERVER_REQUEST_TIMEOUT = float(os.getenv('LLAMA_SERVER_REQUEST_TIMEOUT', 2700))
    
    # ── Unified tool reference ──
    BASE_TOOLS = [
        "<<<REMOTE_EXEC>>>command                          — run a shell command (Linux/macOS compatible)",
        "<<<READ_FILE>>>path                               — read file content (safe, fast)",
        "<<<WRITE_FILE>>>path\\ncontent                     — write content to file (first line = path, rest = content; if the content itself contains <<<TOOL>>> markers, end it with <<<END_FILE>>>)",
        "<<<EDIT_FILE>>>path\\n<<<OLD>>>\\nold text\\n<<<NEW>>>\\nnew text  — surgical edit: find and replace text in file",
        "<<<LIST_DIR>>>path                                — list directory contents with sizes and dates",
        "<<<GLOB>>>pattern                                 — find files matching pattern (e.g., **/*.swift, src/*.py)",
        "<<<GREP>>>pattern|path|options                    — search file contents (options: i=ignore case)",
        "<<<SAVE_MEMORY>>>fact                             — persist a fact",
        "<<<WEB_SEARCH>>>query                             — web search",
        # The tool names are listed because the MCP rejects anything else with
        # "Unknown tool", and nothing else published them — so an agent had to
        # guess, and the service was advertised but unusable (DEV-484).
        # ONLY the six that return content are named. The MCP also exposes
        # WWDC/HIG tools that return search URLs rather than text, and
        # Xcode-local tools that return [] on Linux; naming those would send an
        # agent to burn a turn on a guaranteed empty (the DEV-479 failure).
        '<<<APPLE_DEEP_DOCS>>>{"tool":"NAME","arguments":{}}  — Apple docs (server MCP). Tools:',
        '    search_swift_evolution{feature} | get_swift_evolution_proposal{se_number}  — why a Swift feature exists',
        '    fetch_apple_documentation{url}  — parsed page from developer.apple.com',
        '    fetch_github_file{url}          — file contents from Apple/swiftlang GitHub',
        "<<<INGEST_PDF>>>path                              — ingest a PDF file into memory (supports local: prefix for client files)",
        "<<<SCRATCHPAD>>>                                  — update your working memory (FACTS, OPEN_QUESTIONS, DEAD_ENDS)",
        "<<<PLAN>>>                                        — create/update your retrieval plan (GOAL, STEPS with [x]/[ ], CURRENT)",
        "<<<CONFIDENCE>>>N                                 — report confidence 0-100 in your current information",
    ]

    # ── Combined tools ──
    ALL_TOOLS = BASE_TOOLS

    TOOL_REFERENCE = "# TOOLS — emit these markers inline to execute commands.\n" + "\n".join(ALL_TOOLS)

    # ── Tool reference for reviewer ──
    # Adds a single git-context line to the base reference. The model already
    # knows git/find/grep/diff/wc syntax; the prior 30-line cheat-sheet was
    # ~600 tokens of negative-value prompt mass on every reviewer call.
    GIT_TOOL_REFERENCE = (
        TOOL_REFERENCE +
        "\n\n# Use `<<<REMOTE_EXEC>>>git ...` (status, log, diff, blame, show) to "
        "investigate change history. Use `find` / `grep` / `diff` / `wc` for "
        "navigation and analysis."
    )

    # ── Token budget guidance (injected dynamically) ──
    #
    # Two variants, because the two caller populations can honour very
    # different advice.
    #
    # Interactive clients drive a tool-using agent over many turns, so they can
    # act on "signal continuation" and "assemble the file with shell tools".
    # Programmatic callers (the autonomous pipeline: architect, implementer,
    # reviewer, manifest, per-file, synthesis, supervisor, planner) get ONE
    # shot at a response that a regex then parses. For them:
    #
    #   - There is no continuation turn on THIS path. The interactive client does
    #     honour <<<CONTINUE>>> (orchestrator._split_continue_signal, DEV-80), but
    #     that loop is the client's — the autonomous pipeline calls the API once
    #     and regex-parses the reply. A programmatic caller's model that stops
    #     early to emit the marker produces a short file set, and the reviewer
    #     reports a bogus "missing file" FAIL. Hence: no marker in this variant.
    #   - They have no tools, so "use cat to assemble the file" is unfollowable.
    #   - "Prioritise critical files over auxiliary ones" invites dropping
    #     required files — the same bogus FAIL by another route.
    #
    # So the shared core tells the model to keep each unit whole and to fit by
    # being terser rather than by omitting; only the interactive variant carries
    # the continuation protocol and the tool-based advice.
    _BUDGET_HEADER = """# OUTPUT BUDGET: ~{available_tokens} tokens available for your response.

CRITICAL: Plan your response to fit within this budget."""

    _BUDGET_GUIDELINES = """BUDGET GUIDELINES:
- ~100 tokens ≈ 75 words or ~4-5 lines of code
- A typical function: 50-200 tokens
- A typical file: 200-1000 tokens
- If budget < 1000: Keep response very concise
- If budget < 500: Single focused answer only"""

    # Programmatic / single-shot callers. Marker-safe: no <<<CONTINUE>>>, no
    # tool advice, no instruction that could be read as "omit required output".
    TOKEN_BUDGET_GUIDANCE_CORE = f"""{_BUDGET_HEADER}

1. NEVER TRUNCATE A UNIT: complete each file, function, or section fully before
   starting the next. A half-emitted file is worse than a terse one — whatever
   parses your output treats an unterminated unit as a MISSING unit, not a
   partial one.

2. FIT BY BEING TERSER, NOT BY OMITTING: if the work is close to the budget,
   tighten the output — less boilerplate, fewer comments, no restating the task.
   Do NOT drop, stub, or partially emit anything the task requires, and do NOT
   invent a continuation marker: this is a single-shot request and there is no
   continuation turn. Nothing will ask you for the rest.

{_BUDGET_GUIDELINES}"""

    # Interactive, tool-using clients over a multi-turn session.
    TOKEN_BUDGET_GUIDANCE = f"""{_BUDGET_HEADER} If the task requires more output:

1. PARTITION LARGE TASKS: Break into logical, self-contained sections
   - Each section should be complete and usable on its own
   - For code: complete one file or one function fully before moving on
   - For explanations: complete one topic fully before the next

2. PRIORITIZE: Do the most important/requested work FIRST
   - Core functionality before edge cases
   - Critical files before auxiliary ones
   - Working code before optimizations

3. SIGNAL CONTINUATION: If you cannot finish everything, end with:
   <<<CONTINUE>>>
   REMAINING: [brief list of what still needs to be done]

   The client will automatically request continuation.

4. MAINTAIN ATOMIC INTEGRITY: When the budget won't fit a large file in one turn, DO NOT emit a partial rewrite. Build it up across turns instead: <<<WRITE_FILE>>> a complete, syntactically valid first slice, then extend it with successive <<<EDIT_FILE>>> calls, one block at a time. Never assemble a file with shell commands — the tool rules above forbid it, and for good reason: it skips the diff preview and breaks checkpoint/undo. The worktree must be syntactically valid at the end of EVERY turn.

{_BUDGET_GUIDELINES}"""

    # ── macOS development toolkit (injected into EXECUTOR_PROMPT) ──
    MACOS_TOOLKIT = """
MACOS DEVELOPMENT TOOLKIT — Available via `<<<REMOTE_EXEC>>>`:
You are running on macOS with FULL local access. You CAN and SHOULD write and execute
scripts (python3 / node / swift / ruby), and you have the standard Unix toolchain
(jq, awk, sqlite3, ffmpeg, curl, gh, make/cmake/clang, otool, pdftotext, etc).

macOS-SPECIFIC tools you may not reach for by default:
- mdfind / mdls: Spotlight search and file metadata
- sips: resize/convert/rotate images without ImageMagick
- plutil / textutil: convert plists / docx ↔ JSON/HTML/RTF
- osascript: automate any macOS app via AppleScript
- pbcopy / pbpaste: pipe to/from clipboard
- open: launch files/URLs in their default app
- defaults: read/write macOS preferences
- xcodebuild / xcrun: full Xcode CLI toolchain
- swiftc: Swift compilation

IMPORTANT: For complex tasks, write a python3/node/swift script rather than
chaining shell commands."""

    # ── Behavioral instruction for action-oriented agents ──
    EXECUTOR_PROMPT = """You execute tasks by running commands and writing files.

TOOL SYNTAX: Each tool is a single opening tag. Content runs until the next tool tag.
No closing tags. Just open the next tool (or end your response) to terminate the previous block.

NAVIGATION & SEARCH:
<<<LIST_DIR>>>path
<<<GLOB>>>**/*.swift
<<<GREP>>>pattern|path
<<<READ_FILE>>>path

FILE MODIFICATION:
<<<WRITE_FILE>>>path
content

<<<EDIT_FILE>>>path
<<<OLD>>>
existing code to find
<<<NEW>>>
replacement code

Rules:
- Every response MUST contain at least one tool block
- For NEW files: use <<<WRITE_FILE>>>
- For EXISTING files: prefer <<<EDIT_FILE>>> for targeted changes (safer, shows intent clearly)
- Use <<<WRITE_FILE>>> for existing files only when rewriting most of the file
- NEVER just output code in markdown blocks - that does NOT save anything!
- NEVER use <<<REMOTE_EXEC>>> with Python/sed/awk to modify files. ALWAYS use <<<EDIT_FILE>>> or <<<WRITE_FILE>>> instead. Shell-based file edits bypass safety checks, produce no diff preview, and break checkpoint/undo.
- Use <<<GLOB>>> and <<<GREP>>> to find files instead of shell find/grep (faster, cleaner output)
- After writing/editing files, use <<<REMOTE_EXEC>>> to compile/build and verify changes work
- Reserve <<<REMOTE_EXEC>>> for: builds, tests, git commands, and read-only inspection. NOT for file modification.
- Never ask for permission. You have full file access.
- Never claim you cannot run commands or write files. You can.

CONTEXT MANAGEMENT — your context window is limited. Work efficiently:
- Work FILE-BY-FILE: read a file, modify it, verify it, then move to the next.
  Do NOT read all files before starting work.
- After reading a file, save key findings with <<<SAVE_MEMORY>>> before moving on.
  This lets you drop the raw content from context while retaining what matters.
- Prefer <<<GREP>>> over <<<READ_FILE>>> when you only need to find specific content.

WORKING MEMORY — track your progress across tool calls:
<<<SCRATCHPAD>>>
FACTS:
- list key findings here
OPEN_QUESTIONS:
- what you still need to find
DEAD_ENDS:
- approaches that didn't work

<<<PLAN>>>
GOAL: What you're trying to accomplish
STEPS:
1. [ ] First step
2. [ ] Second step
CURRENT: 1

<<<CONFIDENCE>>>N
Report your confidence (0-100) that you have enough information to answer.
Update these after each retrieval step. They help you stay organized and efficient.
""" + MACOS_TOOLKIT

    # ── Shared model configs ──
    # Turbo: Speed-optimized implementer on RTX 5080.
    # Migrated 2026-04-30 from llama_cpp (ngl=30, 131K Q4_0 KV) to llama_server
    # + cpu_moe. Headroom from cpu_moe redirected to KV-quant upgrade per
    # feedback_kv_quant_preference: 131K Q4_0 → 131K Q8_0, ub bumped to 4096.
    _MOE_30B_TURBO = _create_model_config(
        'MODEL_PATH_30B_TURBO',
        f'{_MODELS_ROOT}/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf',
        49, 131072, 4096,
        server_extra_args=['--chat-template', 'chatml', '--swa-full'],
        logit_bias=[[151657, -100.0], [151658, -100.0]],
        cpu_moe=True, n_ubatch=4096,
    )

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

    # DEVSTRAL: Devstral Small 2 24B Q4_K_M — DEV-414 fast-tier candidate.
    # Dense 24B (mistral3, 40 blocks), so unlike every MoE above it fits
    # almost entirely in the RTX 5080's VRAM: 13.7 GB weights + Q8_0 KV.
    # n_ctx is the tight dimension — 8 KV-heads x 128 x 40 blocks ~= 85 KB/token
    # at Q8_0. 16K ctx measured 358 MiB free (under the vram guard's 500 MiB
    # cushion); 14K keeps the margin above it.
    # --jinja: mistral3 needs its embedded Mistral template, not chatml.
    # No logit_bias: the Qwen tool-call token ids don't exist in this vocab.
    _DENSE_24B_DEVSTRAL = _create_model_config(
        'MODEL_PATH_24B_DEVSTRAL',
        f'{_MODELS_ROOT}/unsloth/Devstral-Small-2-24B-Instruct-2512-GGUF/Devstral-Small-2-24B-Instruct-2512-Q4_K_M.gguf',
        41, 14336, 2048,
        server_extra_args=['--jinja'],
    )

    # NEXT: Qwen3-Coder-Next-Q8_0 (80B MoE with 3B active params)
    # Very smart but runs mostly on system RAM (slow). Native 256k context enabled.
    # ngl=48 (--cpu-moe): 8,304 MiB free. All 48 attention layers on GPU.
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

    # HD: High-precision Q8_0 weights for reviewer-tier judgment.
    # Migrated 2026-04-28 from llama_cpp (ngl=21, 65K Q4_0 KV) to llama_server
    # + cpu_moe. Old layout offloaded 21 of 48 layers — each carrying all 128
    # experts, of which only 8 are active per token — so most GPU bandwidth
    # was wasted reading dead expert weights. cpu_moe keeps attention on GPU
    # and only the 8 active experts per token are read from CPU memory.
    # Q8_0 KV (per feedback_kv_quant_preference) at 196K — native 256K would
    # need ~12.9 GB KV alone. 196K + ub=3072 lands ~1.16 GB free (very tight,
    # at user-sanctioned tolerance); ub=8192 OOMs because compute-buffer
    # scaling is ~1.28 MiB/ub above ub=2048, not the ~0.4 MiB/ub the small-ub
    # observation suggests. Logit-ban for <tool_call>/</tool_call> tokens
    # shared across the Qwen3-Coder family (151657/151658).
    _MOE_30B_HD = _create_model_config(
        'MODEL_PATH_30B_HD',
        f'{_MODELS_ROOT}/lmstudio-community/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q8_0.gguf',
        49, 196608, 4096,
        server_extra_args=['--chat-template', 'chatml', '--swa-full'],
        logit_bias=[[151657, -100.0], [151658, -100.0]],
        cpu_moe=True, n_ubatch=3072,
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

    # ── Qwen3.5 family (deep_reviewer still uses 122B) ──

    # Qwen3.5-122B-A10B Q4_K_M — mid-tier MoE (10B active, 76.5 GB, 3 shards).
    # Retained for `deep_reviewer` only.
    #
    # Migrated 2026-04-28 from llama_cpp (ngl=9, 65K, no cpu_moe) to
    # llama_server + cpu_moe at native 256K context. The previous layout
    # offloaded 9 full layers (incl. 256 dense experts each) to GPU but only
    # 8 experts were active per token, wasting GPU bandwidth. With cpu_moe,
    # attention sublayers go on GPU and ALL experts stay on CPU, reading only
    # the 8 active experts per layer per token. Decode is bandwidth-bound on
    # 10B active params at Q4_K_M ≈ 6 GB/token; DDR5-5600 dual-channel ≈ 90
    # GB/s gives a ~15 tok/s ceiling, vs the ~6 tok/s we measured under the
    # old layout. Architecture: 48 transformer blocks, 3072 dim, 32 attn /
    # 2 KV heads (aggressive GQA → tiny KV cache), 256 experts × 1024 FFN.
    _MOE_122B = _create_model_config(
        'MODEL_PATH_122B',
        f'{_MODELS_ROOT}/unsloth/Qwen3.5-122B-A10B-GGUF/Q4_K_M/Qwen3.5-122B-A10B-Q4_K_M-00001-of-00003.gguf',
        49, 262144, 4096,
        server_extra_args=['--jinja', '--reasoning-format', 'none', '--swa-full'],
        cpu_moe=True, n_ubatch=3072,
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
    _MOE_35B = _create_model_config(
        'MODEL_PATH_35B',
        f'{_MODELS_ROOT}/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf',
        41, 65536, 4608,
        server_extra_args=['--jinja', '--reasoning-format', 'none', '--swa-full'],
        type_k=8, type_v=8,
        cpu_moe=True, n_cpu_moe=20, n_ubatch=4608,
        repeat_penalty=1.05,
    )

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
    # 64 attention layers; dense (no cpu_moe possible). 16 GB VRAM forces
    # partial GPU offload. Per-layer cost ~280 MiB (245 weight + 36 KV at
    # 131K Q4_0). Pushed ngl 20→36→40 across two iterations.
    # Measured 2026-04-24 at ngl=36: 13,513 MiB used, 2,297 free.
    #
    # MTP wired 2026-06-05: model -> unsloth/Qwen3.6-27B-MTP-GGUF (Q4_K_M with the
    # native multi-token-prediction head embedded, +0.29 GB) + `--spec-type
    # draft-mtp --spec-draft-n-max 2`. The dense 27B decodes slowly (~24 of 64
    # layers on CPU); MTP ~doubles it. Measured on build 5343f45 with the prod
    # global flags (lookup-cache + cache-reuse, no conflict): baseline 8.3 tok/s
    # -> MTP 14.1 tok/s decode = ~1.7x, ~85% draft acceptance on structured output.
    # ngl=36 (was 40->38->36): 1,754 free / 13.5 tok/s decode. ngl=40 OOM-tight
    # (714 free); ngl=38 (1,287 free) CRASHED the autonomous architect-revision
    # pass — design-review (#3) swaps back to q36 AND the larger r1 prompt
    # (design + review feedback ~4.6K tok) OOM'd the prefill compute buffer
    # (SIGABRT rc=-6; spec_b956e1c9, 2026-06-13 — see [[project_model_swap_oom]]).
    # 36 restores the headroom that pass needs (~−0.6 tok/s decode is worth it).
    # Quality lossless (verified tokens == base model). Used by dense_architect +
    # supervisor. See [[project_mtp_test_scope]] / [[project_llama_server_upgrade]].
    _DENSE_27B = _create_model_config(
        'MODEL_PATH_27B',
        f'{_MODELS_ROOT}/unsloth/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-Q4_K_M.gguf',
        36, 131072, 2048,
        server_extra_args=['--jinja', '--reasoning-format', 'none', '--swa-full',
                           '--spec-type', 'draft-mtp', '--spec-draft-n-max', '2'],
        type_k=2, type_v=2,
        n_ubatch=2048,
    )

    # ── Non-Coding Model models ──

    # Nemotron-3-Nano-30B-A3B Q4_K_M — NVIDIA hybrid Mamba-Transformer MoE
    # 3.5B active, 24.6 GB. Needs llama_server (nemotron_h_moe arch not in llama-cpp-python).
    # 32K native context. ~3.3x throughput vs Qwen3-30B on same hardware.
    # ngl=28 (no --cpu-moe): 1,964 MiB free | ngl=28 (--cpu-moe): 12,840 MiB free
    # 52 attention layers total. With --cpu-moe, targeting ngl=52 (all layers).
    # Mamba-hybrid: only 6/52 layers use KV cache (rest are recurrent — no KV needed).
    # KV at 1M Q8_0: ~3,264 MiB → ~8.6 GB free. Full 1M native context fits easily.
    _HYBRID_30B = _create_model_config(
        'MODEL_PATH_HYBRID_30B',
        f'{_MODELS_ROOT}/unsloth/Nemotron-3-Nano-30B-A3B-GGUF/Nemotron-3-Nano-30B-A3B-Q4_K_M.gguf',
        52, 1048576, 1024,
        server_extra_args=['--jinja', '--reasoning-format', 'none'],
        cpu_moe=True, n_ubatch=1024,
    )

    # GLM-4.7-Flash Q4_K_M — Zhipu AI 30B-A3B MoE, 18.3 GB
    # Uses llama_server for proper glm4 template handling. 128K native context.
    # Q4_0 cache at 82K ctx. Smallest model — can push most GPU layers.
    # ngl=34 (no --cpu-moe): 884 MiB free | ngl=34 (--cpu-moe): 12,652 MiB free
    # 47 attention layers total. With --cpu-moe, targeting ngl=47 (all layers).
    # KV at 82K Q4_0: 1,164 MiB (47 GPU layers). 12,228 MiB free at ngl=47.
    # Bumping to 262K Q4_0: ~3,713 MiB KV → ~8.5 GB free. Fits easily.
    # KV cache upgraded Q4_0→Q8_0 (9 GB free at Q4_0 — plenty for 2x cache size)
    # Expert-offload tuned 2026-06-03: 262K->64K ctx + n_cpu_moe=20 (27 of 47
    # expert layers on the RTX 5080): measured +74% decode (37->64 tok/s) at 64K,
    # ~1.7 GB VRAM free (GLM's smaller experts offload further than the others).
    _MOE_30B_FLASH = _create_model_config(
        'MODEL_PATH_30B_FLASH',
        f'{_MODELS_ROOT}/unsloth/GLM-4.7-Flash-GGUF/GLM-4.7-Flash-Q4_K_M.gguf',
        47, 65536, 2048,
        # NB: --reasoning-budget 0 was tried here to stop GLM-4.7 burning the
        # whole budget inside <think> as an implementer — it does NOT work: GLM
        # still streams reasoning as plain text (template swallows the opening
        # <think> but the prose leaks into content) and truncates. GLM was
        # therefore dropped from the implementer rotation (see
        # _IMPLEMENTER_ROTATION). This agent config is retained for ad-hoc use.
        server_extra_args=['--jinja', '--reasoning-format', 'none'],
        n_ubatch=2048,
        cpu_moe=True, n_cpu_moe=20,
    )

    # ── Few-shot example injected for executor agents ──
    # The model sees this as a real prior exchange, so it copies the format.
    FEW_SHOT = [
        {"role": "user", "content": "List the Python files in this project."},
        {"role": "assistant", "content": "<<<GLOB>>>**/*.py"},
        {"role": "user", "content": "Find where the login function is defined."},
        {"role": "assistant", "content": "<<<GREP>>>def login|."},
        {"role": "user", "content": "Create a hello world Swift file."},
        {"role": "assistant", "content": "<<<WRITE_FILE>>>/tmp/hello.swift\nimport Foundation\n\nprint(\"Hello, World!\")\n\nNow let me verify it compiles:\n<<<REMOTE_EXEC>>>swiftc /tmp/hello.swift -o /tmp/hello && /tmp/hello"},
        {"role": "user", "content": "Change the greeting from Hello to Hi in that file."},
        {"role": "assistant", "content": "<<<EDIT_FILE>>>/tmp/hello.swift\n<<<OLD>>>\nprint(\"Hello, World!\")\n<<<NEW>>>\nprint(\"Hi, World!\")"},
        {"role": "user", "content": "Parse the API response in /tmp/response.json and extract all user emails."},
        {"role": "assistant", "content": "<<<REMOTE_EXEC>>>jq '[.users[].email]' /tmp/response.json"},
        {"role": "user", "content": "Write a script to find all Swift files over 500 lines and report their line counts."},
        {"role": "assistant", "content": "<<<WRITE_FILE>>>/tmp/long_files.py\nimport subprocess, os\n\nresult = subprocess.run(['find', '.', '-name', '*.swift', '-type', 'f'], capture_output=True, text=True)\nfor path in sorted(result.stdout.strip().split('\\n')):\n    if path:\n        with open(path) as f:\n            count = sum(1 for _ in f)\n        if count > 500:\n            print(f'{count:>6} {path}')\n\n<<<REMOTE_EXEC>>>python3 /tmp/long_files.py"},
        {"role": "user", "content": "Resize all PNG images in assets/ to 512x512."},
        {"role": "assistant", "content": "Let me find the PNGs first:\n<<<GLOB>>>assets/**/*.png\n\nNow I'll resize them using macOS sips:\n<<<REMOTE_EXEC>>>for f in assets/*.png; do sips -z 512 512 \"$f\"; done"},
        {"role": "user", "content": "Find and fix the bug causing 500 errors on the /api/chat endpoint."},
        {"role": "assistant", "content": "<<<PLAN>>>\nGOAL: Find and fix 500 error on /api/chat\nSTEPS:\n1. [ ] Find the endpoint handler\n2. [ ] Trace the error path\n3. [ ] Identify the root cause\n4. [ ] Apply fix and verify\nCURRENT: 1\n\n<<<CONFIDENCE>>>10\n\nLet me start by finding the endpoint:\n<<<GREP>>>/api/chat|."},
    ]

    # ── Shared agent prompts ──
    _IMPLEMENTER_SYSTEM_PROMPT = (
        f'You are an implementer. {EXECUTOR_PROMPT}\n\nCOMPREHENSIVE IMPLEMENTATION: When implementing tasks, leverage multiple tools to understand the codebase thoroughly:\n\nEXECUTION ENVIRONMENT: You are running on a macOS environment with full access to development tools.\n- Use `<<<REMOTE_EXEC>>>` for ALL shell commands (including Xcode tools, Git, file operations).\n- Do NOT distinguish between "server" and "client". Everything runs locally.\n\nFILE OPERATIONS:\n- Use `<<<GLOB>>>` to find files: `<<<GLOB>>>**/*.swift`\n- Use `<<<GREP>>>` to search code: `<<<GREP>>>TODO|src/`\n- Use `<<<LIST_DIR>>>` to explore directories\n- Use `<<<READ_FILE>>>` to read file contents\n- Use `<<<WRITE_FILE>>>` for new files or complete rewrites\n- Use `<<<EDIT_FILE>>>` for targeted changes to existing files (PREFERRED)\n\nGIT AWARENESS: Use Git via `<<<REMOTE_EXEC>>>` to understand code context:\n- `git log`, `git diff`, `git blame`, `git show`, `git status`\n\nAPPLE DEVELOPMENT via `<<<REMOTE_EXEC>>>`:\n- Compile Swift: `swiftc file.swift -o output`\n- Compile Metal: `xcrun -sdk macosx metal -c shader.metal -o shader.air`\n- Build Xcode: `xcodebuild -project Foo.xcodeproj -scheme Foo build`\n\n{TOOL_REFERENCE}'
    )

    # Tools-aware variant — used when the request carries a `tools` array.
    # Drops <<<REMOTE_EXEC>>> marker docs (the model should call the
    # remote_exec function directly via the OpenAI tools interface) and keeps
    # marker docs only for the tools that have NOT been migrated yet.
    _IMPLEMENTER_NATIVE_TOOLS_SYSTEM_PROMPT = (
        'You are an implementer working on a macOS development environment with '
        'full access to development tools, source files, and Git. Take action '
        '— never just describe what you would do.\n\n'
        'TOOL CONVENTIONS — this session uses TWO interfaces:\n\n'
        '1) FUNCTION CALL (use the OpenAI tools interface — do NOT emit these as text):\n'
        '   - `remote_exec(command)` — execute any shell command (builds, tests, '
        'git, ls, grep, sips, xcodebuild, swiftc, etc.). Call this through the '
        'function-call interface. NEVER write `<<<REMOTE_EXEC>>>` as text — that '
        'marker is deprecated for this session.\n\n'
        '2) INLINE MARKERS (emit these tags directly in your response text):\n'
        '   - `<<<READ_FILE>>>path` — read a file\n'
        '   - `<<<LIST_DIR>>>path` — list a directory\n'
        '   - `<<<GLOB>>>**/*.swift` — find files by pattern\n'
        '   - `<<<GREP>>>pattern|path` — search code\n'
        '   - `<<<WRITE_FILE>>>path\\ncontent` — create or rewrite a file\n'
        '   - `<<<EDIT_FILE>>>path\\n<<<OLD>>>existing\\n<<<NEW>>>replacement` — '
        'targeted edit (PREFERRED for changes to existing files)\n'
        '   - `<<<SAVE_MEMORY>>>note` — record a finding\n\n'
        'RULES:\n'
        '- Shell commands → call `remote_exec` (function call). Never the marker.\n'
        '- File modification → use `<<<EDIT_FILE>>>` (preferred) or `<<<WRITE_FILE>>>`. '
        'NEVER modify files via `remote_exec` (no `python -c`, `sed -i`, heredocs, '
        '`>` redirects). Shell-based file edits bypass diff preview, write-loop '
        'detection, and checkpoints.\n'
        '- File inspection → prefer `<<<GLOB>>>` / `<<<GREP>>>` over shell `find` / `grep` (faster, cleaner output).\n'
        '- After writing files, call `remote_exec` to verify (build, run tests).\n'
        '- Every response must produce at least one tool call (function or marker).\n'
        '- Never ask for permission. You have full file access.\n\n'
        'WORKING MEMORY (optional but encouraged for multi-step tasks):\n'
        '<<<SCRATCHPAD>>>\nFACTS:\n- key findings\nOPEN_QUESTIONS:\n- what you still need\n\n'
        '<<<PLAN>>>\nGOAL: ...\nSTEPS:\n1. [ ] first\n2. [ ] second\nCURRENT: 1\n\n'
        '<<<CONFIDENCE>>>0-100\n'
    )

    _ARCHITECT_SYSTEM_PROMPT = (
        f'You are a system architect. {EXECUTOR_PROMPT}\n\n'
        'ROLE: You DESIGN systems and PLAN implementations. You do NOT write large amounts '
        'of code yourself. Your job is to:\n'
        '1. Understand the codebase by reading files, searching, and exploring\n'
        '2. Design the architecture, interfaces, and file structure\n'
        '3. Write a clear, actionable implementation plan\n'
        '4. Create small scaffolding files (configs, interfaces, stubs) if helpful\n'
        '5. Delegate the bulk implementation to an implementer agent\n\n'
        'WHAT YOU SHOULD DO:\n'
        '- Use <<<GLOB>>>, <<<GREP>>>, <<<READ_FILE>>>, <<<LIST_DIR>>> extensively to understand the codebase\n'
        '- Use <<<REMOTE_EXEC>>> to run git log, git diff, git blame for context\n'
        '- Use <<<SAVE_MEMORY>>> to record key findings and design decisions\n'
        '- Write short config files, interface definitions, or type stubs with <<<WRITE_FILE>>>\n'
        '- Write documentation (README, ARCHITECTURE.md, ADRs) with <<<WRITE_FILE>>>\n'
        '- Output a structured implementation plan as your final deliverable\n\n'
        'WHAT YOU SHOULD NOT DO:\n'
        '- Do NOT write large source files (>50 lines). Delegate to an implementer.\n'
        '- Do NOT enter build/test/fix loops. That is implementer work.\n'
        '- Do NOT rewrite files repeatedly. Write once or delegate.\n'
        '- Do NOT run xcodebuild, compilers, or test suites. Leave verification to implementers.\n\n'
        'EDIT_FILE FORMAT (for small targeted edits only):\n'
        '<<<EDIT_FILE>>>path\n<<<OLD>>>\nexact text to find\n<<<NEW>>>\nreplacement text\n\n'
        'WARNING: Do NOT use git-style markers like <<<<<<< SEARCH or ======= or >>>>>>> REPLACE. '
        'Use <<<OLD>>> and <<<NEW>>> only.\n\n'
        f'{TOOL_REFERENCE}'
    )

    _REVIEWER_SYSTEM_PROMPT = (
        f'You are a code reviewer. {EXECUTOR_PROMPT}\n\nIdentify issues and suggest improvements. You are encouraged to provide detailed advice and recommendations.\n\nCOMPREHENSIVE ANALYSIS: When performing code reviews, leverage multiple tools to understand the codebase thoroughly:\n\nGIT AWARENESS: Use Git via `<<<REMOTE_EXEC>>>` to understand code context:\n- `git log`, `git diff`, `git blame`, `git show`, `git status`\n\nFILE NAVIGATION: Use `<<<GLOB>>>` and `<<<GREP>>>` to find and search files.\n\nDOCUMENTATION - You can and should write/update documentation:\n- Use `<<<WRITE_FILE>>>` for NEW documentation files\n- Use `<<<EDIT_FILE>>>` for targeted updates to EXISTING docs (PREFERRED)\n\nEDIT_FILE FORMAT (use EXACTLY this format):\n<<<EDIT_FILE>>>/path/to/file\n<<<OLD>>>\nexact text to find\n<<<NEW>>>\nreplacement text\n\nWARNING: Do NOT use git-style markers like <<<<<<< SEARCH or ======= or >>>>>>> REPLACE. Use <<<OLD>>> and <<<NEW>>> only.\n\nAlways gather comprehensive context before providing your review.\n{GIT_TOOL_REFERENCE}'
    )

    # MiniMax M2.5 generates garbled Unicode (triple-encoded U+FFFD from training
    # data corruption). Force ASCII-only diagrams to prevent hallucinated mojibake.
    _UNICODE_GUARD = (
        '\n\nCRITICAL — FORMATTING RULE: NEVER use Unicode box-drawing characters '
        '(├, └, │, ─, etc.) or Unicode symbols in your output. They WILL render as '
        'garbled text. Use ONLY plain ASCII for diagrams and trees:\n'
        '  +-- for branches\n'
        '  |   for vertical lines\n'
        '  `-- for last items\n'
        'For architecture diagrams, prefer Mermaid syntax in code blocks.\n'
    )

    # ── Agent definitions ──
    # 'executor': True means few-shot + fallback extraction are enabled.
    AGENTS = {
        'implementer': _create_agent_config(
            'Implementer — Qwen3.6-35B-A3B UD-Q4_K_M (3B/35B MoE, 64K ctx Q8_0, ngl=41 n_cpu_moe=20, default)',
            _IMPLEMENTER_SYSTEM_PROMPT,
            _MOE_35B,
            executor=True
        ),
        # `ornith` retired 2026-07-14 after losing the DEV-90 eval to `implementer`
        # (a wash, edge to implementer). See the _MOE_ORNITH retirement note above.
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
        # DEV-414 outcome (2026-08-03, blind counterbalanced, judge=claude-sdk,
        # artifacts var/eval_dev414_*): beat fast_implementer 4-3-1 on quality
        # and finished the 8-task set 16% faster on wall clock via concision
        # (4893 vs 5831 tok) despite 7% slower decode (53.3 vs 57.3 tok/s).
        # Kept registered as the quality-edge option for short-context work;
        # NOT repointing the pipeline's "low" tier — that costs 64K -> 14K ctx,
        # unsafe without prompt-size telemetry for low-tier executor runs.
        'devstral_implementer': _create_agent_config(
            'Implementer — Devstral Small 2 24B Q4_K_M (dense, 14K ctx Q8_0, ngl=41 VRAM-resident, DEV-414 eval)',
            _IMPLEMENTER_SYSTEM_PROMPT,
            _DENSE_24B_DEVSTRAL,
            executor=True
        ),
        # NB: `architect` (the interactive role) is no longer a standalone entry —
        # DEV-99 repointed it at Qwen3.6-27B, making it identical to dense_architect,
        # so DEV-101 folded it into an alias (see AGENT_ALIASES below). `@architect`
        # and `--model architect` still resolve; it just isn't separately listed in
        # /v1/models. Restore a distinct entry here if the interactive architect ever
        # needs to diverge (different model, prompt, or context) from the planner.
        'reviewer': _create_agent_config(
            'Reviewer — Coder-30B Q8_0 (3B/30B MoE, 192K ctx Q8_0, ngl=49 cpu_moe ub=3072, high precision)',
            _REVIEWER_SYSTEM_PROMPT,
            _MOE_30B_HD,
            executor=True
        ),
        'deep_reviewer': _create_agent_config(
            'Reviewer — Qwen3.5-122B Q4_K_M (10B/122B MoE, 256K ctx Q8_0, ngl=49 cpu_moe ub=3072, deep judgment)',
            _REVIEWER_SYSTEM_PROMPT,
            _MOE_122B,
            executor=True
        ),
        'debugger': _create_agent_config(
            'Debugger — Coder-30B Q4_K_M (3B/30B MoE, 128K ctx Q8_0, ngl=49 cpu_moe, turbo)',
            f'You are a debugger. {EXECUTOR_PROMPT}\n\nDEBUGGING WORKFLOW:\n- Use `<<<READ_FILE>>>` to examine source code\n- Use `<<<REMOTE_EXEC>>>` to run tests, check logs, execute debuggers\n- Use `<<<WRITE_FILE>>>` to apply fixes to source files\n- After fixing, use `<<<REMOTE_EXEC>>>` to verify the fix works (compile, run tests)\n\n{TOOL_REFERENCE}',
            _MOE_30B_TURBO,
            executor=True
        ),
        'moe_implementer': _create_agent_config(
            'Implementer — MiniMax M2.5 Q4_K_M (10B/230B MoE, 116K ctx Q4_0, ngl=62 cpu_moe)',
            _IMPLEMENTER_SYSTEM_PROMPT + _UNICODE_GUARD,
            _MOE_230B,
            executor=True
        ),
        'moe_architect': _create_agent_config(
            'Architect — MiniMax M2.5 Q4_K_M (10B/230B MoE, 116K ctx Q4_0, ngl=62 cpu_moe)',
            _ARCHITECT_SYSTEM_PROMPT + _UNICODE_GUARD,
            _MOE_230B,
            executor=True
        ),
        # ── Qwen3.6 agents (replaced retired Qwen3.5 architect tier) ──
        'dense_architect': _create_agent_config(
            'Architect — Qwen3.6-27B MTP Q4_K_M (27B dense, 128K Q4_0 ctx, ngl=36 + MTP spec-decode, default planner + interactive architect — the `architect` alias)',
            _ARCHITECT_SYSTEM_PROMPT,
            _DENSE_27B,
            executor=True
        ),
        # Same GGUF, same prompt, reasoning block suppressed — the second arm of
        # the DEV-556 head-to-head. Qwen3.6's template gates <think> behind
        # enable_thinking, and we never sent it, so max_tokens has always been a
        # thinking-PLUS-design budget of which only the design half is visible:
        # the reasoning is generated, counted by the server's usage, and then
        # dropped by strip_thinking before call_agent sees a byte of it. Run 12
        # spent 16000 completion tokens and 30.5 minutes to emit ~500 tokens of
        # design, then wrote the whole design in 5369 on the identical prompt
        # (DEV-543). 6 of 20 architect calls across runs 10-12 truncated, every
        # one exactly at the ceiling.
        #
        # NOT yet the incumbent. Fourteen good designs came out of this model
        # WITH thinking on and we have no sample of it designing without, so
        # cheaper is not yet known to be as good — see DEV-93/DEV-99 for two
        # times the measurement contradicted the intuition here. Repoint
        # AUTONOMOUS_ARCHITECT_AGENT only once the judged eval says so.
        'dense_architect_nothink': _create_agent_config(
            'Architect (no reasoning) — Qwen3.6-27B MTP Q4_K_M, enable_thinking=False (DEV-556 eval arm; identical to dense_architect otherwise)',
            _ARCHITECT_SYSTEM_PROMPT,
            _DENSE_27B,
            executor=True,
            chat_template_kwargs={'enable_thinking': False},
        ),
        # Supervisor — meta-orchestrator. Always invoked with native tools
        # (decide()), never with marker-based shell tools, so executor=False.
        'supervisor': _create_agent_config(
            'Supervisor — Qwen3.6-27B MTP Q4_K_M (27B dense, decision-only, no shell tools)',
            _SUPERVISOR_SYSTEM_PROMPT,
            _DENSE_27B,
        ),
        # ── Non-Coding Model agents ──
        'brainstorm': _create_agent_config(
            'Brainstorm — Nemotron-3-Nano Q4_K_M (3.5B/30B Mamba-MoE, 1M ctx Q8_0, ngl=52 cpu_moe, fastest, no tools)',
            'You are a fast brainstorming assistant. Help the user think through ideas, '
            'explore approaches, outline plans, and draft designs. You are great at rapid '
            'iteration and generating options quickly.\n\n'
            'IMPORTANT: You do NOT have access to tools, files, or shell commands. '
            'Do NOT output <<<WRITE_FILE>>>, <<<REMOTE_EXEC>>>, or any tool markers. '
            'Do NOT fabricate file contents or command outputs. If the user asks you to '
            'read, write, or execute something, tell them to switch to the implementer agent.\n\n'
            'Focus on: brainstorming, planning, outlining, comparing approaches, drafting '
            'pseudocode, explaining concepts, and reviewing ideas.',
            _HYBRID_30B,
        ),
        'native_implementer': _create_agent_config(
            'Implementer — GLM-4.7-Flash Q4_K_M (3B/30B MoE, 64K ctx Q8_0, ngl=47 n_cpu_moe=20, Zhipu AI)',
            _IMPLEMENTER_SYSTEM_PROMPT,
            _MOE_30B_FLASH,
            executor=True,
            system_prompt_native_tools=_IMPLEMENTER_NATIVE_TOOLS_SYSTEM_PROMPT,
        ),
    }

    # Backward-compat: old model-versioned agent names → new role/tier keys.
    # Keeps existing .env defaults (AUTONOMOUS_*_AGENT), saved sessions, and
    # @-mentions working after the de-branding rename. Aliases are NOT listed
    # in /v1/models — they only resolve on lookup.
    AGENT_ALIASES = {
        # `architect` = the interactive architect role. Folded into dense_architect
        # in DEV-101 once DEV-99 made them the same model+prompt. Not listed in
        # /v1/models, but still resolves for @-mentions and --model architect.
        'architect':       'dense_architect',
        # Autonomous architect handle. Repointed to the thinking-off arm by
        # DEV-562's clean 6-task eval (nothink 3-2-1, −23% tokens/wall, zero
        # truncations, no degenerate answers under a tools-free prompt) per
        # DEV-556's pre-registered wins-or-ties criterion. The INTERACTIVE
        # 'architect' alias above deliberately keeps thinking on: without the
        # private channel the model reaches for tools first, which interactive
        # use services and the autonomous single-call path cannot.
        'q36_architect':   'dense_architect_nothink',
        'm25_architect':   'moe_architect',
        'm25_implementer': 'moe_implementer',
        'glm':             'native_implementer',
        'nemotron':        'brainstorm',
    }

    @classmethod
    def resolve_agent(cls, name):
        """Map a possibly-legacy agent name to its canonical key.

        Returns the canonical name when `name` is a known alias, otherwise
        returns `name` unchanged (callers still do their own existence check).
        """
        return cls.AGENT_ALIASES.get(name, name)

    @classmethod
    def validate(cls) -> List[str]:
        """Validate configuration before starting server"""
        errors = []
        if not 1 <= cls.PORT <= 65535:
            errors.append(f"PORT must be between 1 and 65535, got: {cls.PORT}")
            
        for agent, config in cls.AGENTS.items():
            path = config['model_config']['path']
            if not os.path.exists(path):
                errors.append(f"Model for {agent} not found: {path}")
                
        return errors
