/*
 * Copyright (c) 2026 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import assert from 'node:assert/strict';
import { after, before, mock, test } from 'node:test';
import { staticClient } from 'fumadocs-core/search/client/orama-static';
import { createSearchAPI, type AdvancedIndex } from 'fumadocs-core/search/server';
import { createDocsSearchClient, getSearchCategory, type SearchScope } from '../src/lib/docs-search';
import type { Language } from '../src/lib/i18n';
import { withBasePath } from '../src/lib/urls';

const languages: Language[] = ['en', 'zh'];
const queries = { en: 'search', zh: '搜索' };

function page(locale: Language, path: string, title: string): AdvancedIndex {
  const url = `/${locale}/${path}`;
  return {
    id: url,
    url,
    title,
    locale,
    tag: path.split('/')[0],
    structuredData: { headings: [], contents: [] },
  };
}

const fixtures = languages.flatMap((locale) => {
  const query = queries[locale];
  const verboseTitle = `${query} ${'configuration settings examples overview reference '.repeat(20)}`;
  return [
    ...Array.from({ length: 75 }, (_, index) => page(locale, `rfcs/proposal-${index}`, query)),
    page(locale, 'docs/less-relevant', verboseTitle),
    page(locale, 'docs/more-relevant', query),
    page(locale, 'development/less-relevant', verboseTitle),
    page(locale, 'development/more-relevant', query),
    {
      ...page(locale, 'docs/develop/integration', 'Integration guide'),
      structuredData: {
        headings: Array.from({ length: 6 }, (_, index) => ({
          id: `section-${index}`,
          content: `${query} section ${index}`,
        })),
        contents: Array.from({ length: 6 }, (_, index) => ({
          heading: `section-${index}`,
          content: `${query} ${query} content ${index}`,
        })),
      },
    },
    page(locale, 'other/unclassified', `${query} discoverability`),
    page(locale, 'rfcs/discoverability', 'discoverability'),
    page(locale, 'development/discoverability', 'discoverability'),
    page(locale, 'docs/discoverability', 'discoverability'),
  ];
});

before(async () => {
  const api = createSearchAPI('advanced', {
    indexes: fixtures,
    localeFilter: true,
    sort: { enabled: false },
  });
  const exported = await (await api.staticGET()).text();
  mock.method(globalThis, 'fetch', async (input: string | URL | Request) => {
    assert.ok(languages.some((locale) => String(input) === withBasePath(`/${locale}/search-index.json`)));
    return new Response(exported, { headers: { 'content-type': 'application/json' } });
  });
});

after(() => mock.restoreAll());

for (const locale of languages) {
  test(`${locale}: classifies exact documentation roots and their descendants`, () => {
    for (const category of ['docs', 'development', 'rfcs'] as const) {
      for (const suffix of ['', '/', '/nested/page', '/nested/page?query=1#heading']) {
        assert.equal(getSearchCategory(`/${locale}/${category}${suffix}`), category);
        assert.equal(getSearchCategory(withBasePath(`/${locale}/${category}${suffix}`)), category);
      }
    }
    assert.equal(getSearchCategory(`/${locale}/docs/develop/integration`), 'docs');
    for (const path of ['docs-other', 'develop', 'modules/runtime', 'api/reference', 'other/docs/page']) {
      assert.equal(getSearchCategory(`/${locale}/${path}`), undefined);
    }
  });

  test(`${locale}: all prioritizes user docs before more than 60 RFC matches`, async () => {
    const unscoped = await staticClient({
      from: withBasePath(`/${locale}/search-index.json`),
      locale,
      search: { groupBy: { properties: ['page_id'], maxResult: 2 } },
    }).search(queries[locale]);
    assert.equal(unscoped.length, 60);
    assert.ok(unscoped.every((result) => getSearchCategory(result.url) === 'rfcs'));

    const results = await createDocsSearchClient(locale, 'all').search(queries[locale]);
    const pages = results.filter((result) => result.type === 'page');
    const categories = pages.map((result) => getSearchCategory(result.url));
    assert.equal(results.length, 60);
    assert.ok(pages.some((result) => result.url === `/${locale}/docs/less-relevant`));
    assert.equal(categories[0], 'docs');
    assert.ok(categories.includes('development'));
    assert.ok(categories.includes('rfcs'));
    for (const category of ['docs', 'development']) {
      const urls = pages.map((result) => result.url);
      assert.ok(urls.indexOf(`/${locale}/${category}/more-relevant`) < urls.indexOf(`/${locale}/${category}/less-relevant`));
    }
    assert.deepEqual(categories, [...categories].sort((a, b) => {
      const rank = { docs: 0, development: 1, rfcs: 2 };
      return rank[a!] - rank[b!];
    }));
  });

  for (const scope of ['docs', 'development', 'rfcs'] as const) {
    test(`${locale}: ${scope} filters before truncation and preserves relevance`, async () => {
      const results = await createDocsSearchClient(locale, scope).search(queries[locale]);
      assert.ok(results.length > 0);
      assert.ok(results.every((result) => getSearchCategory(result.url) === scope));
      const shortResults = await createDocsSearchClient(locale, scope).search('discoverability');
      assert.deepEqual(shortResults.map((result) => result.url), [`/${locale}/${scope}/discoverability`]);
      if (scope !== 'rfcs') {
        const urls = results.filter((result) => result.type === 'page').map((result) => result.url);
        assert.ok(urls.includes(`/${locale}/${scope}/less-relevant`));
        assert.ok(urls.indexOf(`/${locale}/${scope}/more-relevant`) < urls.indexOf(`/${locale}/${scope}/less-relevant`));
      }
    });
  }

  test(`${locale}: displays at most two matching snippets per page`, async () => {
    const results = await createDocsSearchClient(locale, 'docs').search(queries[locale]);
    const url = `/${locale}/docs/develop/integration`;
    const snippets = results.filter((result) => result.type !== 'page' && result.url.split('#')[0] === url);
    assert.equal(results.filter((result) => result.type === 'page' && result.url === url).length, 1);
    assert.equal(snippets.length, 2);
    assert.ok(snippets.every((result) => result.url.startsWith(`${url}#section-`)));
  });

  test(`${locale}: all keeps unclassified matches after the known categories`, async () => {
    const results = await createDocsSearchClient(locale, 'all').search('discoverability');
    assert.deepEqual(results.filter((result) => result.type === 'page').map((result) => result.url), [
      `/${locale}/docs/discoverability`,
      `/${locale}/development/discoverability`,
      `/${locale}/rfcs/discoverability`,
      `/${locale}/other/unclassified`,
    ]);
  });

  test(`${locale}: every scope keeps the other locale out`, async () => {
    for (const scope of ['all', 'docs', 'development', 'rfcs'] satisfies SearchScope[]) {
      const results = await createDocsSearchClient(locale, scope).search('');
      assert.ok(results.length > 0);
      assert.ok(results.every((result) => result.url.startsWith(`/${locale}/`)));
    }
  });
}

test('does not classify unsupported or missing language prefixes', () => {
  for (const url of ['/fr/docs/guide', '/docs/guide', '/en', 'en/docs/guide', '/en?path=/docs/guide']) {
    assert.equal(getSearchCategory(url), undefined);
  }
});
