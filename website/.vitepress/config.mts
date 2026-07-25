import { defineConfig } from "vitepress";

export default defineConfig({
  title: "qbx",
  description: "Debrid companion for qBittorrent — Control Shell, interceptor, and desktop tray",
  lang: "en-US",
  base: "/qbittorrent_debrid/",
  cleanUrls: true,
  lastUpdated: true,
  head: [
    ["link", { rel: "icon", href: "/qbittorrent_debrid/favicon.svg", type: "image/svg+xml" }],
  ],
  themeConfig: {
    logo: "/logo.svg",
    siteTitle: "qbx",
    nav: [
      { text: "Install", link: "/install/" },
      { text: "Guides", link: "/guides/" },
      { text: "CLI", link: "/cli/" },
      { text: "Config", link: "/configuration/" },
      { text: "API", link: "/api/" },
      {
        text: "More",
        items: [
          { text: "Troubleshooting", link: "/troubleshooting/" },
          { text: "Contributing", link: "/contributing/" },
          { text: "GitHub", link: "https://github.com/oldrepublicwizard/qbittorrent_debrid" },
        ],
      },
    ],
    sidebar: {
      "/install/": [
        {
          text: "Install",
          items: [
            { text: "Overview", link: "/install/" },
            { text: "Quick start", link: "/install/quick" },
            { text: "Desktop install", link: "/install/desktop" },
            { text: "Docker", link: "/install/docker" },
          ],
        },
      ],
      "/guides/": [
        {
          text: "Guides",
          items: [
            { text: "Overview", link: "/guides/" },
            { text: "Control Shell", link: "/guides/control-shell" },
            { text: "Debrid flow", link: "/guides/debrid" },
            { text: "File matching", link: "/guides/matching" },
            { text: "Native tray", link: "/guides/tray" },
            { text: "Updates", link: "/guides/updates" },
            { text: "systemd", link: "/guides/systemd" },
          ],
        },
      ],
      "/cli/": [{ text: "CLI", link: "/cli/" }],
      "/configuration/": [
        {
          text: "Configuration",
          items: [
            { text: "Overview", link: "/configuration/" },
            { text: "Environment variables", link: "/configuration/env" },
          ],
        },
      ],
      "/api/": [{ text: "HTTP API", link: "/api/" }],
      "/troubleshooting/": [{ text: "Troubleshooting", link: "/troubleshooting/" }],
      "/contributing/": [{ text: "Contributing", link: "/contributing/" }],
    },
    socialLinks: [
      { icon: "github", link: "https://github.com/oldrepublicwizard/qbittorrent_debrid" },
    ],
    footer: {
      message: "Local-first debrid helper for qBittorrent.",
      copyright: "qbx contributors · MIT",
    },
    search: { provider: "local" },
  },
});
