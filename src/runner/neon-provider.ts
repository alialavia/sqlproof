import type { NeonOptions } from '../schema/types.js';

const NEON_API = 'https://console.neon.tech/api/v2';

export interface NeonBranchResult {
  branchId: string;
  connectionString: string;
}

async function neonFetch<T>(
  apiKey: string,
  method: 'GET' | 'POST' | 'DELETE',
  path: string,
  body?: unknown,
): Promise<T> {
  const res = await fetch(`${NEON_API}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(
      `Neon API ${res.status} ${res.statusText} — ${method} ${path}: ${text}`,
    );
  }

  return res.json() as Promise<T>;
}

export async function createNeonBranch(opts: NeonOptions): Promise<NeonBranchResult> {
  const { apiKey, projectId, database = 'neondb', role = 'neondb_owner', parentBranch } = opts;

  const branchName = `sqlproof-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

  const createBody: {
    branch: { name: string; parent_id?: string };
    endpoints: [{ type: 'read_write' }];
  } = {
    branch: { name: branchName },
    endpoints: [{ type: 'read_write' }],
  };
  if (parentBranch) createBody.branch.parent_id = parentBranch;

  const createRes = await neonFetch<{ branch: { id: string } }>(
    apiKey,
    'POST',
    `/projects/${projectId}/branches`,
    createBody,
  );

  const branchId = createRes.branch.id;

  const uriRes = await neonFetch<{ uri: string }>(
    apiKey,
    'GET',
    `/projects/${projectId}/connection_uri?branch_id=${encodeURIComponent(branchId)}&role_name=${encodeURIComponent(role)}&database_name=${encodeURIComponent(database)}`,
  );

  return { branchId, connectionString: uriRes.uri };
}

export async function deleteNeonBranch(
  apiKey: string,
  projectId: string,
  branchId: string,
): Promise<void> {
  await neonFetch(apiKey, 'DELETE', `/projects/${projectId}/branches/${branchId}`);
}
