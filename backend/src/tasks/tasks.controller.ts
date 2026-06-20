import { Body, Controller, Get, NotFoundException, Param, Post, Res } from '@nestjs/common';
import type { Response } from 'express';
import * as path from 'path';
import * as fs from 'fs';
import { TasksService } from './tasks.service';
import { CreateTaskDto } from './dto/create-task.dto';
import { PrismaService } from '../prisma/prisma.service';

// Mismo volumen montado en docker-compose.yml para backend y worker.
// El worker escribe aquí (ARTIFACTS_DIR), NestJS solo lee.
const ARTIFACTS_ROOT = path.resolve(process.env.ARTIFACTS_DIR || '/app/artifacts');

@Controller('tasks')
export class TasksController {
  constructor(
    private readonly tasksService: TasksService,
    private readonly prisma: PrismaService,
  ) {}

  @Post()
  create(@Body() dto: CreateTaskDto) {
    return this.tasksService.create(dto);
  }

  @Get()
  findAll() {
    return this.tasksService.findAll();
  }

  @Get(':id')
  findOne(@Param('id') id: string) {
    return this.tasksService.findOne(id);
  }

  @Get(':taskId/artifacts/:artifactId/download')
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
