import type {
  HealthResponse,
  ModelListResponse,
  Spec,
  SpecDetailResponse,
  GateRespondRequest,
  ApiResponse,
  MetricsResponse,
  GpuStatsResponse,
  ActiveModelResponse,
} from '../types/api';

// API host defaults to the same host that served the dashboard, on port 5000.
// In dev (Vite at localhost:3000) → http://localhost:5000. In production
// served at http://192.168.50.101:3001 → http://192.168.50.101:5000. On a
// TV pointed at the LAN dashboard → same. Override with VITE_CODING_MODEL_SERVER_URL
// at build time if the coding-model-server lives on a different host or port.
const BASE_URL =
  import.meta.env.VITE_CODING_MODEL_SERVER_URL ||
  `${window.location.protocol}//${window.location.hostname}:5000`;

function getAdminKey(): string | null {
  return localStorage.getItem('codingModel.adminKey');
}

async function request<T>(url: string, options?: RequestInit): Promise<ApiResponse<T>> {
  const key = getAdminKey();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(key ? { 'X-Admin-Key': key } : {}),
    ...((options?.headers as Record<string, string>) || {}),
  };

  try {
    const response = await fetch(`${BASE_URL}${url}`, {
      ...options,
      headers,
    });

    if (!response.ok) {
      return { error: `HTTP ${response.status}: ${response.statusText}` };
    }

    const data = await response.json();
    return { data };
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Network error';
    return { error: message };
  }
}

export async function fetchHealth(): Promise<ApiResponse<HealthResponse>> {
  return request<HealthResponse>('/health');
}

export async function fetchModels(): Promise<ApiResponse<ModelListResponse>> {
  return request<ModelListResponse>('/v1/models');
}

export async function fetchSpecs(limit = 50): Promise<ApiResponse<Spec[]>> {
  return request<Spec[]>(`/v1/autonomous/specs?limit=${limit}`);
}

export async function fetchSpecDetail(id: string): Promise<ApiResponse<SpecDetailResponse>> {
  return request<SpecDetailResponse>(`/v1/autonomous/specs/${id}`);
}

export async function respondToGate(gateId: string, payload: GateRespondRequest): Promise<ApiResponse<void>> {
  return request<void>(`/v1/autonomous/gates/${gateId}/respond`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function fetchMetrics(windowSeconds = 60): Promise<ApiResponse<MetricsResponse>> {
  return request<MetricsResponse>(`/v1/admin/metrics?window_seconds=${windowSeconds}`);
}

export async function fetchGpuStats(): Promise<ApiResponse<GpuStatsResponse>> {
  return request<GpuStatsResponse>('/v1/admin/gpu_stats');
}

export async function fetchActiveModel(): Promise<ApiResponse<ActiveModelResponse>> {
  return request<ActiveModelResponse>('/v1/admin/active_model');
}