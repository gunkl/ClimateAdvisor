const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({
  testDir: './tests/ui',
  timeout: 15000,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: 'http://localhost:7531',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: {
    command: 'node tests/ui/mock-server.js',
    port: 7531,
    reuseExistingServer: false,
    timeout: 5000,
  },
});
