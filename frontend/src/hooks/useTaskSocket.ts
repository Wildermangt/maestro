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

export function useTaskSocket(onTaskCompleted: (event: TaskCompletedEvent) => void) {
  const [connected, setConnected] = useState(false);
  const socketRef = useRef<Socket | null>(null);
  const callbackRef = useRef(onTaskCompleted);
  callbackRef.current = onTaskCompleted;

  useEffect(() => {
    const socket = io(`${WS_URL}/tasks`, {
      transports: ['websocket'],
    });

    socket.on('connect', () => setConnected(true));
    socket.on('disconnect', () => setConnected(false));
    socket.on('task:completed', (event: TaskCompletedEvent) => {
      callbackRef.current(event);
    });

    socketRef.current = socket;

    return () => {
      socket.disconnect();
    };
  }, []);

  return { connected };
}
