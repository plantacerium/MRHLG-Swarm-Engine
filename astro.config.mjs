import { defineConfig } from 'astro/config';

// https://astro.build/config
export default defineConfig({
  output: 'static',
  srcDir: './src',
  outDir: './dist',
  server: {
    port: 4321,
    host: 'localhost',
  },
  vite: {
    server: {
      proxy: {},
    },
  },
});
