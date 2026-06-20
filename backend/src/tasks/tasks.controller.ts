import { Body, Controller, Get, NotFoundException, Param, Post, Res } from '@nestjs/common';
import { ApiOperation, ApiParam, ApiResponse, ApiTags } from '@nestjs/swagger';
import type { Response } from 'express';
import * as path from 'path';
import * as fs from 'fs';
import { TasksService } from './tasks.service';
import { CreateTaskDto } from './dto/create-task.dto';
import { PrismaService } from '../prisma/prisma.service';

// Mismo volumen montado en docker-compose.yml para backend y worker.
// El worker escribe aquí (ARTIFACTS_DIR), NestJS solo lee.
const ARTIFACTS_ROOT = path.resolve(process.env.ARTIFACTS_DIR || '/app/artifacts');

@ApiTags('tasks')
@Controller('tasks')
export class TasksController {
  constructor(
    private readonly tasksService: TasksService,
    private readonly prisma: PrismaService,
  ) {}

  @Post()
  @ApiOperation({
    summary: 'Crear una tarea y encolarla para un agente',
    description:
      'Crea la tarea en Postgres y la encola en BullMQ. El worker Python la ' +
      'procesa de forma asíncrona; el resultado llega por WebSocket ' +
      '(namespace /tasks, evento task:completed) y queda también disponible ' +
      'vía GET /tasks/:id una vez completada.',
  })
  @ApiResponse({ status: 201, description: 'Tarea creada y encolada (status PROCESSING)' })
  @ApiResponse({ status: 400, description: 'Body inválido (ver class-validator)' })
  create(@Body() dto: CreateTaskDto) {
    return this.tasksService.create(dto);
  }

  @Get()
  @ApiOperation({ summary: 'Listar las últimas 50 tareas, más reciente primero' })
  findAll() {
    return this.tasksService.findAll();
  }

  @Get(':id')
  @ApiOperation({ summary: 'Obtener una tarea por id, incluyendo sus artefactos' })
  @ApiParam({ name: 'id', description: 'UUID de la tarea' })
  @ApiResponse({ status: 404, description: 'Tarea no encontrada' })
  findOne(@Param('id') id: string) {
    return this.tasksService.findOne(id);
  }

  @Get(':taskId/artifacts/:artifactId/download')
  @ApiOperation({
    summary: 'Descargar un artefacto generado (PPTX, HTML, etc.)',
    description:
      'Sirve el archivo desde el volumen compartido con el worker. ' +
      'Valida que la ruta resuelta no se salga de ARTIFACTS_ROOT.',
  })
  @ApiParam({ name: 'taskId', description: 'UUID de la tarea propietaria del artefacto' })
  @ApiParam({ name: 'artifactId', description: 'UUID del artefacto' })
  @ApiResponse({ status: 200, description: 'Descarga del archivo binario' })
  @ApiResponse({ status: 404, description: 'Artefacto o archivo no encontrado' })
  async downloadArtifact(
    @Param('taskId') taskId: string,
    @Param('artifactId') artifactId: string,
    @Res() res: Response,
  ) {
    const artifact = await this.prisma.artifact.findUnique({ where: { id: artifactId } });

    if (!artifact || artifact.taskId !== taskId) {
      throw new NotFoundException('Artefacto no encontrado');
    }

    // artifact.url contiene el relativePath interno (ver TasksService.createArtifacts).
    // Se resuelve contra ARTIFACTS_ROOT y se verifica que el resultado siga
    // DENTRO de esa carpeta — defensa explícita contra path traversal,
    // incluso aunque relativePath hoy solo lo genera nuestro propio worker.
    const resolvedPath = path.resolve(ARTIFACTS_ROOT, artifact.url);
    if (!resolvedPath.startsWith(ARTIFACTS_ROOT + path.sep)) {
      throw new NotFoundException('Ruta de artefacto inválida');
    }

    if (!fs.existsSync(resolvedPath)) {
      throw new NotFoundException('El archivo del artefacto ya no existe en disco');
    }

    res.download(resolvedPath, artifact.filename);
  }
}
