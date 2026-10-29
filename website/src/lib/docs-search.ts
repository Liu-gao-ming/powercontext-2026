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

import type { SearchClient } from 'fumadocs-core/search/client';
import { staticClient } from 'fumadocs-core/search/client/orama-static';
import type { Language } from './i18n';
import { basePath, withBasePath } from './urls';

export type SearchScope = 'all' | 'docs' | 'development' | 'rfcs';

export function getSearchCategory(url: string): Exclude<SearchScope, 'all'> | undefined {
  let pathname = url.split(/[?#]/, 1)[0];
  if (basePath && pathname.startsWith(`${basePath}/`)) pathname = pathname.slice(basePath.length);
  const category = /^\/(?:en|zh)\/(docs|development|rfcs)(?:\/|$)/.exec(pathname)?.[1];
  return category as Exclude<SearchScope, 'all'> | undefined;
}

const categoryPriority = { docs: 0, development: 1, rfcs: 2 };

export function createDocsSearchClient(locale: Language, scope: SearchScope): SearchClient {
  return staticClient({
    from: withBasePath(`/${locale}/search-index.json`),
    locale,
    tag: scope === 'all' ? undefined : scope,
    search: {
      // Sort every match before the engine groups pages and Fumadocs applies its result limit.
      sortBy: (a, b) => {
        const categoryA = getSearchCategory(a[2].url);
        const categoryB = getSearchCategory(b[2].url);
        const priorityA = categoryA ? categoryPriority[categoryA] : 3;
        const priorityB = categoryB ? categoryPriority[categoryB] : 3;
        return priorityA - priorityB || b[1] - a[1];
      },
      groupBy: { properties: ['page_id'], maxResult: 2 },
    },
  });
}
