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
        { label: 'Getting Started', slug: 'getting-started' },
        {
          label: 'API Reference',
          items: [
            { label: 'SqlProof Class', slug: 'api/sqlproof-class' },
            { label: 'CheckOptions', slug: 'api/check-options' },
            { label: 'TableCustomization', slug: 'api/table-customization' },
          ],
        },
        {
          label: 'Guides',
          items: [
            { label: 'FK Distribution Strategies', slug: 'guides/fk-distributions' },
            { label: 'Custom Generators', slug: 'guides/custom-generators' },
            { label: 'CI/CD Integration', slug: 'guides/ci-cd' },
            { label: 'Security & Credentials', slug: 'guides/security' },
            { label: 'Local Development', slug: 'guides/local-dev' },
          ],
        },
        {
          label: 'Examples',
          items: [{ label: 'E-Commerce Orders', slug: 'examples/orders' }],
        },
      ],
    }),
  ],
});
