import { Body, Controller, Delete, Get, Param, Patch, Post, Query } from '@nestjs/common';
import { ApiOperation, ApiQuery, ApiTags } from '@nestjs/swagger';
import { EntityStatus } from '@prisma/client';
import { EntidadEntrante, MemoryService } from './memory.service';

@ApiTags('memory')
@Controller('memory')
export class MemoryController {
  constructor(private readonly memoria: MemoryService) {}

  @Post('entities')
  @ApiOperation({
    summary: 'Guardar o fusionar entidades encontradas por los agentes',
    description:
      'Deduplica por dominio del sitio web. Un campo que ya tiene valor nunca ' +
      'se sobrescribe con uno vacío: una ejecución que no encontró el teléfono ' +
      'no borra el que sí encontró la anterior.',
  })
  guardarEntidades(@Body() body: { entities: EntidadEntrante[]; userId?: string }) {
    return this.memoria.guardarEntidades(body?.entities ?? [], body?.userId);
  }

  @Get('entities')
  @ApiOperation({
    summary: 'Recuperar lo que ya se sabe',
    description:
      'Permite a un agente preguntar "¿qué empresas de este tipo ya tengo?" ' +
      'antes de volver a buscarlas, y evita re-trabajar los mismos leads.',
  })
  @ApiQuery({ name: 'search', required: false })
  @ApiQuery({ name: 'status', required: false, enum: EntityStatus })
  @ApiQuery({ name: 'limit', required: false, type: Number })
  buscarEntidades(
    @Query('search') search?: string,
    @Query('status') status?: EntityStatus,
    @Query('limit') limit?: string,
    @Query('userId') userId?: string,
  ) {
    return this.memoria.buscarEntidades({
      search,
      status,
      limit: limit ? parseInt(limit, 10) : undefined,
      userId,
    });
  }

  @Patch('entities/:id')
  @ApiOperation({ summary: 'Cambiar el estado de una entidad (nuevo, contactado, descartado...)' })
  actualizarEstado(
    @Param('id') id: string,
    @Body() body: { status: EntityStatus; score?: number },
  ) {
    return this.memoria.actualizarEstado(id, body.status, body.score);
  }

  @Get('notes')
  @ApiOperation({ summary: 'Hechos que el sistema recuerda entre tareas' })
  listarNotas(@Query('userId') userId?: string) {
    return this.memoria.listarNotas(userId);
  }

  @Post('notes')
  @ApiOperation({
    summary: 'Recordar un hecho',
    description:
      'Por ejemplo key="cliente_ideal", value="constructoras en Bogotá con ' +
      'obra nueva". Se inyecta como contexto al planificar tareas futuras.',
  })
  guardarNota(@Body() body: { key: string; value: string; userId?: string }) {
    return this.memoria.guardarNota(body.key, body.value, body.userId);
  }

  @Delete('notes/:key')
  @ApiOperation({ summary: 'Olvidar un hecho' })
  borrarNota(@Param('key') key: string, @Query('userId') userId?: string) {
    return this.memoria.borrarNota(key, userId);
  }
}
