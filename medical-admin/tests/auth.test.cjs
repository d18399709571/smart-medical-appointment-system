// Execute the real TypeScript/script-setup logic with storage, HTTP and Vue doubles.
// No browser, backend, environment files or external services are used.
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')

function load(relative, dependencies, globals = {}, expose = []) {
  const filename = path.join(__dirname, '..', relative)
  let source = fs.readFileSync(filename, 'utf8')
  if (relative.endsWith('.vue')) source = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  })
  const module = { exports: {} }
  vm.runInNewContext(outputText + (expose.length ? `\nmodule.exports = { ${expose.join(', ')} };` : ''), {
    module, exports: module.exports, ...globals,
    require(name) {
      assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency: ${name}`)
      return dependencies[name]
    },
  }, { filename })
  return module.exports
}

function fixture(post = async () => {}) {
  const stored = new Map()
  const notices = []
  const routes = []
  const admin = { value: { role: 'doctor' } }
  const localStorage = {
    getItem: (key) => stored.get(key) ?? null,
    setItem: (key, value) => stored.set(key, value),
    removeItem: (key) => stored.delete(key),
  }
  const ElMessage = Object.fromEntries(['success', 'warning', 'error'].map((level) => [
    level, (message) => notices.push({ level, message }),
  ]))
  const request = {
    post,
    interceptors: { request: { use() {} }, response: { use() {} } },
  }
  const storage = load('src/utils/request.ts', {
    axios: { create: () => request }, 'element-plus': { ElMessage },
  }, { localStorage })
  const api = load('src/api/auth.ts', { '../utils/request': storage })
  const router = { push: async (route) => routes.push(route) }
  const shared = {
    vue: { computed: (fn) => ({ get value() { return fn() } }), ref: (value) => ({ value }), reactive: (value) => value },
    'vue-router': { useRouter: () => router, useRoute: () => ({ path: '/profile' }) },
    'element-plus': { ElMessage },
    '../../utils/request': storage,
    '../../stores/auth': { currentAdmin: admin, clearCurrentAdmin: () => { admin.value = null } },
    '../../utils/roleAccess': { getRoleLabel: (role) => role, getRoleHomePath: () => '/my-appointments' },
  }
  return { stored, notices, routes, admin, storage, api, shared }
}

test('admin access and refresh storage are paired; unrelated storage is preserved', () => {
  const { storage, stored } = fixture()
  stored.set('patient_refresh_token', 'patient-session')
  storage.setToken('admin-access')
  storage.setRefreshToken('admin-refresh')
  assert.equal(storage.getToken(), 'admin-access')
  assert.equal(storage.getRefreshToken(), 'admin-refresh')
  storage.clearToken()
  assert.equal(storage.getToken(), null)
  assert.equal(storage.getRefreshToken(), null)
  assert.equal(stored.get('patient_refresh_token'), 'patient-session')
})

test('logout API sends the current refresh token to the existing backend endpoint', async () => {
  const calls = []
  const { api } = fixture(async (url, data) => calls.push([url, data.refresh_token]))
  await api.logout('current-refresh')
  assert.deepEqual(calls, [['/auth/logout', 'current-refresh']])
})

test('successful login saves both tokens before loading admin identity', async () => {
  const f = fixture()
  const page = load('src/views/login/Index.vue', {
    ...f.shared,
    '../../api/auth': { login: async () => ({ access_token: 'new-access', refresh_token: 'new-refresh' }) },
    '../../stores/auth': { loadCurrentAdmin: async () => {
      assert.equal(f.storage.getToken(), 'new-access')
      assert.equal(f.storage.getRefreshToken(), 'new-refresh')
      return { role: 'doctor' }
    } },
  }, {}, ['handleLogin', 'form', 'loading'])
  page.form.email = 'test@example.invalid'
  page.form.password = 'test-password'
  await page.handleLogin()
  assert.equal(page.loading.value, false)
  assert.deepEqual(f.routes, ['/my-appointments'])
})

test('rejected login does not save a session', async () => {
  const f = fixture()
  const page = load('src/views/login/Index.vue', {
    ...f.shared,
    '../../api/auth': { login: async () => { throw new Error('rejected') } },
  }, {}, ['handleLogin', 'form', 'loading'])
  page.form.email = 'test@example.invalid'
  page.form.password = 'wrong-password'
  await assert.rejects(page.handleLogin(), /rejected/)
  assert.equal(page.loading.value, false)
  assert.equal(f.storage.getToken(), null)
  assert.equal(f.storage.getRefreshToken(), null)
  assert.equal(f.routes.length, 0)
})

test('logout waits for server response before clearing local identity', async () => {
  let finish
  const pending = new Promise((resolve) => { finish = resolve })
  const f = fixture(async (url, data) => {
    assert.equal(url, '/auth/logout')
    assert.equal(data.refresh_token, 'current-refresh')
    await pending
  })
  f.storage.setToken('current-access')
  f.storage.setRefreshToken('current-refresh')
  const page = load('src/views/layout/Index.vue', { ...f.shared, '../../api/auth': f.api }, {}, ['handleLogout'])
  const work = page.handleLogout()
  assert.equal(f.storage.getToken(), 'current-access')
  assert.equal(f.storage.getRefreshToken(), 'current-refresh')
  finish()
  await work
  assert.equal(f.storage.getToken(), null)
  assert.equal(f.storage.getRefreshToken(), null)
  assert.equal(f.admin.value, null)
  assert.deepEqual(f.routes, ['/login'])
  assert.deepEqual(f.notices.map((notice) => notice.level), ['success'])
})

for (const scenario of ['network failure', 'missing refresh token']) {
  test(`${scenario}: local logout succeeds without claiming server revocation`, async () => {
    let calls = 0
    const f = fixture(async () => { calls++; throw new Error('offline') })
    f.storage.setToken('current-access')
    if (scenario === 'network failure') f.storage.setRefreshToken('current-refresh')
    const page = load('src/views/layout/Index.vue', { ...f.shared, '../../api/auth': f.api }, {}, ['handleLogout'])
    await page.handleLogout()
    assert.equal(calls, scenario === 'network failure' ? 1 : 0)
    assert.equal(f.storage.getToken(), null)
    assert.equal(f.storage.getRefreshToken(), null)
    assert.equal(f.admin.value, null)
    assert.deepEqual(f.routes, ['/login'])
    assert.deepEqual(f.notices.map((notice) => notice.level), ['warning'])
    assert.match(f.notices[0].message, /本地退出.*未确认/)
  })
}
