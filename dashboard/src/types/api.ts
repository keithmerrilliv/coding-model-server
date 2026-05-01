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