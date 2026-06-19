const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:4000';

export type TaskType = 'RESEARCH' | 'ANALYSIS' | 'PRESENTATION' | 'WEBSITE' | 'DIRECTOR';
export type TaskStatus = 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED' | 'PARTIAL';

export interface Task {
  id: string;
  type: TaskType;
  prompt: string;
  status: TaskStatus;
  progress: number;
  result: Record<string, unknown> | null;
  createdAt: string;
  completedAt: string | null;
}

export async function createTask(input: { type: TaskType; prompt: string }): Promise<Task> {
  const res = await fetch(`${API_URL}/api/tasks`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.message || `Error creando tarea: ${res.status}`);
  }

  return res.json();
}

export async function listTasks(): Promise<Task[]> {
  const res = await fetch(`${API_URL}/api/tasks`);
  if (!res.ok) throw new Error(`Error listando tareas: ${res.status}`);
  return res.json();
}
