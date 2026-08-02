import { Injectable, Logger } from '@nestjs/common';
import { Prisma, EntityStatus } from '@prisma/client';
import { PrismaService } from '../prisma/prisma.service';

const TEST_USER_ID = '00000000-0000-0000-0000-000000000001';

/** Campos de datos que se fusionan al reencontrar una entidad. */
const CAMPOS_FUSIONABLES = [
  'sector',
  'city',
  'website',
  'phone',
  'email',
  'address',
  'products',
  'notes',
] as const;

export interface EntidadEntrante {
  domain?: string;
  name: string;
  sector?: string;
  city?: string;
  website?: string;
  phone?: string;
  email?: string;
  address?: string;
  products?: string;
  notes?: string;
  sources?: Record<string, string>;
}

@Injectable()
export class MemoryService {
  private readonly logger = new Logger(MemoryService.name);

  constructor(private readonly prisma: PrismaService) {}

  /**
   * Normaliza un sitio web a su dominio: es la clave de deduplicación.
   * El nombre de una empresa varía entre fuentes ("MPS", "MPS Mayorista
   * S.A.S."), el dominio no.
   */
  static dominioDe(website?: string, name?: string): string | null {
    const crudo = (website || '').trim();
    if (crudo) {
      try {
        const url = crudo.startsWith('http') ? crudo : `https://${crudo}`;
        const host = new URL(url).hostname.toLowerCase().replace(/^www\./, '');
        if (host.includes('.')) return host;
      } catch {
        // sigue al fallback por nombre
      }
    }
    // Sin sitio web utilizable, se cae al nombre normalizado. Es peor clave
    // (dos variantes del mismo nombre no se fusionan), pero es preferible a
    // descartar la entidad o a crear duplicados sin control.
    const porNombre = (name || '')
      .toLowerCase()
      .normalize('NFD')
      .replace(/[̀-ͯ]/g, '')
      .replace(/\b(s\.?a\.?s?|ltda|inc|corp|cia|s\.?a\.?)\b/g, '')
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '');
    return porNombre ? `nombre:${porNombre}` : null;
  }

  /**
   * Inserta o fusiona entidades.
   *
   * Regla de fusión: un campo que YA tiene valor nunca se sobrescribe con
   * uno vacío. Una segunda ejecución que no encontró el teléfono no debe
   * borrar el que sí encontró la primera. Los campos con valor nuevo sí
   * ganan, porque suelen venir de una fuente más específica (el
   * Enriquecedor visitó el sitio; el Investigador solo lo mencionó).
   */
  async guardarEntidades(entidades: EntidadEntrante[], userId = TEST_USER_ID) {
    let creadas = 0;
    let fusionadas = 0;
    const omitidas: string[] = [];

    for (const entrante of entidades) {
      const domain = MemoryService.dominioDe(entrante.website, entrante.name);
      if (!domain || !entrante.name?.trim()) {
        omitidas.push(entrante.name || '(sin nombre)');
        continue;
      }

      const existente = await this.prisma.entity.findUnique({
        where: { userId_domain: { userId, domain } },
      });

      const datos: Record<string, unknown> = {};
      for (const campo of CAMPOS_FUSIONABLES) {
        const nuevo = (entrante[campo] || '').toString().trim();
        if (!nuevo) continue;
        const previo = (existente?.[campo] || '').toString().trim();
        // Solo se pisa un valor existente si el nuevo es más informativo.
        if (!previo || nuevo.length > previo.length) datos[campo] = nuevo;
      }

      if (!existente) {
        await this.prisma.entity.create({
          data: {
            userId,
            domain,
            name: entrante.name.trim(),
            ...datos,
            sources: (entrante.sources ?? {}) as Prisma.InputJsonValue,
          },
        });
        creadas++;
      } else {
        await this.prisma.entity.update({
          where: { id: existente.id },
          data: {
            ...datos,
            timesSeen: { increment: 1 },
            sources: {
              ...((existente.sources as Record<string, string>) ?? {}),
              ...(entrante.sources ?? {}),
            } as Prisma.InputJsonValue,
          },
        });
        fusionadas++;
      }
    }

    this.logger.log(
      `Memoria: ${creadas} entidad(es) nueva(s), ${fusionadas} fusionada(s), ${omitidas.length} omitida(s)`,
    );
    return { creadas, fusionadas, omitidas };
  }

  /**
   * Recupera lo que ya se sabe. `search` filtra por nombre, sector o
   * ciudad — es lo que permite a un agente preguntar "¿qué empresas de
   * este tipo ya tengo?" antes de salir a buscarlas de nuevo.
   */
  async buscarEntidades(params: {
    search?: string;
    status?: EntityStatus;
    limit?: number;
    userId?: string;
  }) {
    const { search, status, limit = 100, userId = TEST_USER_ID } = params;

    const where: Prisma.EntityWhereInput = { userId };
    if (status) where.status = status;
    if (search?.trim()) {
      const q = search.trim();
      where.OR = [
        { name: { contains: q, mode: 'insensitive' } },
        { sector: { contains: q, mode: 'insensitive' } },
        { city: { contains: q, mode: 'insensitive' } },
        { products: { contains: q, mode: 'insensitive' } },
      ];
    }

    return this.prisma.entity.findMany({
      where,
      orderBy: [{ timesSeen: 'desc' }, { lastSeenAt: 'desc' }],
      take: Math.min(limit, 500),
    });
  }

  async actualizarEstado(id: string, status: EntityStatus, score?: number) {
    return this.prisma.entity.update({
      where: { id },
      data: { status, ...(score !== undefined ? { score } : {}) },
    });
  }

  // --- Memoria de hechos ---------------------------------------------------

  async guardarNota(key: string, value: string, userId = TEST_USER_ID) {
    return this.prisma.memoryNote.upsert({
      where: { userId_key: { userId, key } },
      create: { userId, key, value },
      update: { value },
    });
  }

  async listarNotas(userId = TEST_USER_ID) {
    return this.prisma.memoryNote.findMany({
      where: { userId },
      orderBy: { key: 'asc' },
    });
  }

  async borrarNota(key: string, userId = TEST_USER_ID) {
    await this.prisma.memoryNote.deleteMany({ where: { userId, key } });
    return { borrada: key };
  }
}
