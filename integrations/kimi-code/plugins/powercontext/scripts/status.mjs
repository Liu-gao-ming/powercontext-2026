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

import { parseArgs } from 'node:util'
import { queryObservations } from '../shared/observations.mjs'
import { loadSettings } from '../shared/runtime.mjs'

try {
  const { values } = parseArgs({ options: { cwd: { type: 'string' }, 'session-id': { type: 'string' } } })
  console.log(JSON.stringify(await queryObservations(
    { cwd: values.cwd, session_id: values['session-id'] }, loadSettings({ credentials: false }))))
} catch (error) {
  const code = ['workspace_required', 'invalid_session', 'invalid_state', 'runtime_capacity_exceeded', 'invalid_settings']
    .includes(error.code ?? error.message) ? error.code ?? error.message : 'runtime_unavailable'
  console.log(JSON.stringify({ schema: 'powercontext.kimi-code.runtime-status.v1', status: 'failed', code }))
  process.exitCode = 1
}
