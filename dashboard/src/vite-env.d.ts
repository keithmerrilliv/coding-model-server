/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_CODING_MODEL_SERVER_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}