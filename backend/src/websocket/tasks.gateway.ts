import { Logger } from '@nestjs/common';
import {
  OnGatewayConnection,
  OnGatewayDisconnect,
  WebSocketGateway,
  WebSocketServer,
} from '@nestjs/websockets';
import { Server, Socket } from 'socket.io';

@WebSocketGateway({
  cors: {
    origin: process.env.FRONTEND_URL || 'http://localhost:3000',
  },
  namespace: 'tasks',
})
export class TasksGateway implements OnGatewayConnection, OnGatewayDisconnect {
  private readonly logger = new Logger(TasksGateway.name);

  @WebSocketServer()
  server: Server;

  handleConnection(client: Socket) {
    this.logger.log(`Cliente WS conectado: ${client.id}`);
  }

  handleDisconnect(client: Socket) {
    this.logger.log(`Cliente WS desconectado: ${client.id}`);
  }

  /**
   * Emite a TODOS los clientes conectados. Para el Walking Skeleton esto
   * es suficiente (un solo usuario de prueba). En producción (Sprint 2+)
   * se debe emitir a una room específica por userId:
   *   this.server.to(`user:${userId}`).emit(...)
   */
  emitTaskCompleted(taskId: string, payload: Record<string, unknown>) {
    this.server.emit('task:completed', { taskId, ...payload });
  }

  /**
   * Progreso de una subtarea individual del Director (Sprint 3).
   * Payload shape: ver models.SubTaskProgressEvent en el worker Python.
   */
  emitSubtaskProgress(payload: Record<string, unknown>) {
    this.server.emit('task:subtask-progress', payload);
  }
}
