import type {
  HealthResponse,
  ModelListResponse,
  Spec,
  SpecDetailResponse,
  GateRespondRequest,
  ApiResponse,
} from '../types/api';

const BASE_URL = import.meta.env.VITE_QWEN_SERVER_URL || 'http://localhost:5000';

function getAdminKey(): string | null {
  return localStorage.getItem('qwen.adminKey');
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