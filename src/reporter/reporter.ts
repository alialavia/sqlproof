import type { Dataset } from '../schema/types.js';

export interface RunDetails {
  numRuns: number;
  seed: number;
  numShrinks: number;
}

/**
 * Formats a counterexample as a human-readable string with Unicode box tables.
 */
export function formatCounterexample(
  propertyName: string,
  dataset: Dataset | undefined,
  runDetails: RunDetails,
): string {
  const lines: string[] = [];

  lines.push(`✗ Property failed: "${propertyName}"`);
  lines.push('');
  lines.push(`  After ${runDetails.numRuns} run(s) (seed: ${runDetails.seed})`);
  lines.push('');
  lines.push(
    `  Counterexample (shrunk ${runDetails.numShrinks} time(s)):`,
  );

  if (dataset) {
    for (const [tableName, rows] of Object.entries(dataset)) {
      lines.push('');
      lines.push(`  Table: ${tableName}`);
      if (rows.length === 0) {
        lines.push('  (empty)');
        continue;
      }
      const tableStr = renderTable(rows);
      for (const line of tableStr.split('\n')) {
        lines.push(`  ${line}`);
      }
    }
  }

  lines.push('');
  lines.push(`  Reproduce: sqlproof.check({ ..., seed: ${runDetails.seed} })`);

  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// Unicode box-drawing table renderer
// ---------------------------------------------------------------------------

function renderTable(rows: Record<string, unknown>[]): string {
  if (rows.length === 0) return '(empty)';

  const cols = Object.keys(rows[0]!);
  if (cols.length === 0) return '(no columns)';

  // Compute column widths: max of header and all values
  const widths = cols.map(col => {
    const headerLen = col.length;
    const maxValLen = rows.reduce((max, row) => {
      const v = formatValue(row[col]);
      return Math.max(max, v.length);
    }, 0);
    return Math.max(headerLen, maxValLen);
  });

  const lines: string[] = [];

  // Top border
  lines.push('┌' + widths.map(w => '─'.repeat(w + 2)).join('┬') + '┐');

  // Header row
  lines.push(
    '│' +
      cols.map((col, i) => ` ${col.padEnd(widths[i]!)} `).join('│') +
      '│',
  );

  // Header/body separator
  lines.push('├' + widths.map(w => '─'.repeat(w + 2)).join('┼') + '┤');

  // Data rows
  for (const row of rows) {
    lines.push(
      '│' +
        cols.map((col, i) => ` ${formatValue(row[col]).padEnd(widths[i]!)} `).join('│') +
        '│',
    );
  }

  // Bottom border
  lines.push('└' + widths.map(w => '─'.repeat(w + 2)).join('┴') + '┘');

  return lines.join('\n');
}

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return 'NULL';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}
