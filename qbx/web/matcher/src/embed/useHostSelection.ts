import { useEffect, useState } from "react"
import { ControlApi, type TorrentInfo } from "@/api/backend"
import { bridge } from "@/embed/bridge"

/**
 * Tracks the torrent selected in qBittorrent's own table, kept in sync over
 * the bridge rather than owning any selection state of its own — the host
 * table is the single source of truth.
 */
export function useHostSelection(initialHash?: string | null) {
  const [activeHash, setActiveHash] = useState<string | null>(initialHash ?? null)
  const [torrent, setTorrent] = useState<TorrentInfo | null>(null)

  useEffect(
    () =>
      bridge.onHost((msg) => {
        if (msg.type === "qbx.host.selection") setActiveHash(msg.activeHash)
        else if (msg.type === "qbx.host.panel" && msg.hash) setActiveHash(msg.hash)
        else if (msg.type === "qbx.selectTorrent") setActiveHash(msg.hash)
      }),
    [],
  )

  useEffect(() => {
    if (!activeHash) {
      setTorrent(null)
      return
    }
    let cancelled = false
    ControlApi.getTorrent(activeHash)
      .then((t) => {
        if (!cancelled) setTorrent(t)
      })
      .catch(() => {
        if (!cancelled) setTorrent(null)
      })
    return () => {
      cancelled = true
    }
  }, [activeHash])

  return { activeHash, torrent }
}
