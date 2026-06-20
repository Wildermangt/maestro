'use client';

import { useEffect, useRef, useState } from 'react';
import { io, Socket } from 'socket.io-client';

const WS_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:4000';

export interface TaskCompletedEvent {
  taskId: string;
  status: string;
  result: Record<string, unknown>;
  [key: string]: unknown;
}

export interface SubtaskProgressEvent {
  taskId: string;
  subtaskId: string;
  agentType: string;
  status: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED';
  prompt: string;
  result?: Record<string, unknown> | null;
  error?: string | null;
}

export function useTaskSocket(
  onTaskCompleted: (event: TaskCompletedEvent) => void,
  onSubtaskProgress?: (event: SubtaskProgressEvent) => void,
) {
  const [connected, setConnected] = useState(false);
  const socketRef = useRef<Socket | null>(null);
  const completedRef = useRef(onTaskCompleted);
  completedRef.current = onTaskCompleted;
  const progressRef = useRef(onSubtaskProgress);
  progressRef.current = onSubtaskProgress;

  useEffect(() => {
    const socket = io(`${WS_URL}/tasks`, {
      transports: ['websocket'],
    });

    socket.on('connect', () => setConnected(true));
    socket.on('disconnect', () => setConnected(false));
    socket.on('task:completed', (event: TaskCompletedEvent) => {
      completedRef.current(event);
    });
    socket.on('task:subtask-progress', (event: SubtaskProgressEvent) => {
      progressRef.current?.(event);
    });

    socketRef.current = socket;

    return () => {
      socket.disconnect();
    };
  }, []);

  return { connected };
}
