// Leaving the page for another address, in one place so a test can watch it.
export const browser = {
  assign: (url: string) => location.assign(url),
}
