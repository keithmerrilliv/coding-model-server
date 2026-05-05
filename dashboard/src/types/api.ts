export interface HealthResponse {
  status: string;
  model_loaded: boolean;
  agents: string[];
  timestamp: string;
}

export interface ModelInfo {
  id: string;
  object: string;
  created?: number;
  owned_by?: string;
}

export interface ModelListResponse {
  object: "list";
  data: ModelInfo[];
}

export interface Spec {
  id: string;
  title: string;
  source_md_path: string;
  normalized_yaml: string | null;
  status: string;
  jira_epic_key: string | null;
  created_at: string;
  updated_at: string;
}

export interface Gate {
  id: string;
  spec_id: string;
  task_id: string | null;
  gate_type: string;
  prompt_md: string;
  status: string;
  reviewer_decision: string | null;
  reviewer_notes: string | null;
  jira_issue_key: string | null;
  created_at: string;
  responded_at: string | null;
}

export interface Event {
  id: number;
  spec_id: string;
  task_id: string | null;
  gate_id: string | null;
  kind: string;
  payload_json: string;
  created_at: string;
}

export interface SpecDetailResponse {
  spec: Spec;
  open_gates: Gate[];
  task_count: number;
  recent_events: Event[];
}

export interface GateRespondRequest {
  decision: "approved" | "rejected";
  notes?: string;
}

export interface ApiResponse<T> {
  data?: T;
  error?: string;
}

export interface MetricBucket {
  t_offset: number;
  count: number;
  p50_ms: number | null;
  p95_ms: number | null;
  errors: number;
}

export interface EndpointMetric {
  method: string;
  path: string;
  subkey: string | null;
  total_count: number;
  last_seen_seconds_ago: number | null;
  window_count: number;
  buckets: MetricBucket[];
}

export interface MetricsResponse {
  now: string;
  window_seconds: number;
  endpoints: EndpointMetric[];
}

export interface GpuSample {
  t: string;
  util_gpu: number | null;
  util_mem: number | null;
  vram_used_mib: number | null;
  vram_total_mib: number | null;
  power_w: number | null;
  power_limit_w: number | null;
  clock_gr_mhz: number | null;
  clock_mem_mhz: number | null;
  temp_c: number | null;
  pstate: string | null;
}

export interface GpuStatsResponse {
  available: boolean;
  interval_s: number;
  power_limit_w: number | null;
  vram_total_mib: number | null;
  samples: GpuSample[];
}

export interface ActiveModelDraft {
  path: string | null;
  basename: string | null;
  n_gpu_layers: number | null;
  n_ctx: number | null;
  cpu_moe: boolean;
}

export interface ActiveModelConfig {
  n_ctx: number | null;
  n_batch: number | null;
  n_ubatch: number | null;
  n_gpu_layers: number | null;
  cpu_moe: boolean;
  cache_type_k: string | null;
  cache_type_v: string | null;
  repeat_penalty: number | null;
  repeat_last_n: number | null;
  draft: ActiveModelDraft | null;
}

export interface ActiveModelResponse {
  running: boolean;
  agent_id: string | null;
  model_path: string | null;
  model_basename: string | null;
  pid: number | null;
  idle_timeout_s: number;
  active_requests: number;
  last_request_seconds_ago: number | null;
  uptime_seconds: number | null;
  config: ActiveModelConfig | null;
  agent_description: string | null;
  agent_executor: boolean | null;
  available_agents: string[];
}