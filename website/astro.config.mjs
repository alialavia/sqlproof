import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: 'https://sqlproof.com',
  integrations: [
    starlight({
      title: 'SqlProof',
      social: {
        github: 'https://github.com/alialavia/sqlproof',
      },
      customCss: ['./src/styles/custom.css'],
      sidebar: [
        { label: 'Test your Supabase project in 60s', slug: 'supabase-quickstart' },
        { label: 'Getting Started (general)', slug: 'getting-started' },
        {
          label: 'Test Patterns',
          items: [
            { label: 'Five Property Patterns', slug: 'examples/property-patterns' },
            { label: 'Testing SQL Functions', slug: 'examples/testing-sql-functions' },
            { label: 'Stateful Tests', slug: 'api/state-machine' },
            { label: 'Realistic Data Generation', slug: 'examples/data-generation' },
            { label: 'E-Commerce Orders Walkthrough', slug: 'examples/orders' },
          ],
        },
        {
          label: 'Supabase',
          items: [
            { label: 'Testing Supabase Apps', slug: 'guides/supabase' },
          ],
        },
        {
          label: 'API Reference',
          items: [
            { label: 'SqlProof Class', slug: 'api/sqlproof-class' },
            { label: 'CheckOptions', slug: 'api/check-options' },
            { label: 'TableCustomization', slug: 'api/table-customization' },
          ],
        },
        {
          label: 'Power-User Guides',
          items: [
            { label: 'FK Distribution Strategies', slug: 'guides/fk-distributions' },
            { label: 'Custom Generators', slug: 'guides/custom-generators' },
            { label: 'CI/CD Integration', slug: 'guides/ci-cd' },
            { label: 'Local Development', slug: 'guides/local-dev' },
            { label: 'Security & Credentials', slug: 'guides/security' },
            { label: 'SqlProof vs pgTAP', slug: 'guides/vs-pgtap' },
          ],
        },
      ],
    }),
  ],
});
