const { defineConfig } = require('@playwright/test');
const PORT = Number(process.env.MOCK_PORT) || 7531; // MOCK_PORT lets parallel worktrees run the UI specs side by side
module.exports = defineConfig({
  testDir: './tests/ui',
  timeout: 15000,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: `http://localhost:${PORT}`,
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: {
    command: 'node tests/ui/mock-server.js',
    port: PORT,
    reuseExistingServer: false,
    timeout: 5000,
  },
});
