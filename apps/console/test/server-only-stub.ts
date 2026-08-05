// `server-only` is a Next.js build-time marker that throws if a module is
// imported from a client bundle. It has no runtime behaviour, so unit tests
// alias it to this empty module rather than pulling in the bundler.
export {};
