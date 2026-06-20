import { Injectable, Logger, NotFoundException } from '@nestjs/common';
import { InjectQueue } from '@nestjs/bullmq';
import { Queue } from 'bullmq';
import { PrismaService } from '../prisma/prisma.service';
import { CreateTaskDto } from './dto/create-task.dto';

const TEST_USER_ID = '00000000-0000-0000-0000-000000000001';

@Injectable()
export class TasksService {
  private readonly logger = new Logger(TasksService.name);

  constructor(
    private readonly prisma: PrismaService,
    @InjectQueue('tasks-queue') private readonly tasksQueue: Queue,
  ) {}

  async create(dto: CreateTaskDto) {
    const userId = dto.userId ?? TEST_USER_ID;

    const task = await this.prisma.task.create({
      data: {
        userId,
        type: dto.type,
        prompt: dto.prompt,
        metadata: dto.metadata ?? {},
        status: 'PENDING',
      },
    });

    // El payload que cruza a Python sigue el contrato en shared/contracts.md
    //
    // IMPORTANTE: no pasar opciones de prioridad/delay/attempts en .add() —
    // el worker Python (workers/bullmq_client.py) consume la cola con un
    // cliente Redis casero que asume jobs simples en una lista FIFO.
    // Prioridades usan un ZSET interno en BullMQ y romperían ese consumer.
    // Ver la nota de limitación en bullmq_client.py antes de cambiar esto.
    await this.tasksQueue.add('process-task', {
      taskId: task.id,
      type: task.type,
      prompt: task.prompt,
      userId: task.userId,
      metadata: task.metadata ?? {},
    });

    this.logger.log(`Tarea ${task.id} creada y encolada (type=${task.type})`);

    await this.prisma.task.update({
      where: { id: task.id },
      data: { status: 'PROCESSING' },
    });

    return task;
  }

  async findOne(id: string) {
    const task = await this.prisma.task.findUnique({
      where: { id },
      include: { artifacts: true },
    });

    if (!task) {
      throw new NotFoundException(`Tarea ${id} no encontrada`);
    }

    return task;
  }

  async findAll() {
    return this.prisma.task.findMany({
      orderBy: { createdAt: 'desc' },
      take: 50,
      include: { artifacts: true },
    });
  }

  async markCompleted(taskId: string, result: unknown, status: 'COMPLETED' | 'FAILED' | 'PARTIAL') {
    return this.prisma.task.update({
      where: { id: taskId },
      data: {
        status,
        result: result as any,
        progress: 100,
        completedAt: new Date(),
      },
    });
  }

  /**
   * Crea los registros Artifact a partir de lo que el worker reportó
   * en TaskResult.artifacts (ver shared/contracts.md). `url` apunta al
   * endpoint de descarga propio en vez de a una ruta de archivo directa
   * — así, si en el futuro el storage migra a S3/MinIO (Sprint 5), el
   * frontend nunca necesita cambiar, solo el endpoint de descarga.
   */
  async createArtifacts(
    taskId: string,
    artifacts: Array<{ type: string; filename: string; relativePath: string }>,
  ) {
    const created = await Promise.all(
      artifacts.map((a) =>
        this.prisma.artifact.create({
          data: {
            taskId,
            type: a.type,
            filename: a.filename,
            // Se guarda la relativePath en `url` temporalmente; el valor
            // público real se construye en el controller a partir del id.
            url: a.relativePath,
          },
        }),
      ),
    );

    return created.map((a) => ({
      id: a.id,
      type: a.type,
      filename: a.filename,
      url: `/api/tasks/${taskId}/artifacts/${a.id}/download`,
    }));
  }
}
