import { useState, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Plus, Trash2, Edit2, Check, X, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { MatcherService, QbtServiceExt, type MatcherRule } from "@/api/backend";
import { ControlApi } from "@/api/backend";
import type { HealthInfo } from "@/api/backend";

interface MatcherSurfaceProps {
  health?: HealthInfo | null;
}

export function MatcherSurface({ health }: MatcherSurfaceProps) {
  const [rules, setRules] = useState<MatcherRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [categories, setCategories] = useState<string[]>([]);
  const [savePaths, setSavePaths] = useState<string[]>([]);
  const [editingRule, setEditingRule] = useState<MatcherRule | null>(null);
  const [globalEnabled, setGlobalEnabled] = useState(false);
  const [autoPlacement, setAutoPlacement] = useState(false);
  const [intervalMinutes, setIntervalMinutes] = useState(60);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [rulesRes, cats, paths, config] = await Promise.all([
        MatcherService.GetRules(),
        QbtServiceExt.GetCategories(),
        QbtServiceExt.GetSavePaths(),
        ControlApi.getConfig(),
      ]);
      setRules(rulesRes);
      setCategories(cats);
      setSavePaths(paths);
      const matcherConfig = config.matcher || {};
      setGlobalEnabled(matcherConfig.enabled !== false);
      setAutoPlacement(matcherConfig.auto_placement !== true);
      setIntervalMinutes(matcherConfig.interval_minutes || 60);
    } catch (err) {
      toast.error(`Failed to load matcher data: ${err}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const saveRule = async (rule: MatcherRule) => {
    if (!rule.name.trim()) {
      toast.error("Rule name is required");
      return;
    }
    if (!rule.search_path.trim()) {
      toast.error("Search path is required");
      return;
    }

    setSaving(true);
    try {
      const existingRules = rules.filter((r) => r.name !== rule.name);
      if (editingRule) {
        // Update existing
        const updatedRules = [...existingRules, rule].sort((a, b) => a.priority - b.priority);
        await MatcherService.UpdateRules(updatedRules);
        setRules(updatedRules);
      } else {
        // Add new
        const updatedRules = [...rules, rule].sort((a, b) => a.priority - b.priority);
        await MatcherService.UpdateRules(updatedRules);
        setRules(updatedRules);
      }
      setEditingRule(null);
      toast.success(`Rule ${rule.name} saved`);
    } catch (err) {
      toast.error(`Failed to save rule: ${err}`);
    } finally {
      setSaving(false);
    }
  };

  const deleteRule = async (name: string) => {
    if (!confirm(`Delete rule "${name}"?`)) return;
    setSaving(true);
    try {
      const updatedRules = rules.filter((r) => r.name !== name);
      await MatcherService.UpdateRules(updatedRules);
      setRules(updatedRules);
      toast.success(`Rule ${name} deleted`);
    } catch (err) {
      toast.error(`Failed to delete rule: ${err}`);
    } finally {
      setSaving(false);
    }
  };

  const saveGlobalSettings = async () => {
    setSaving(true);
    try {
      await ControlApi.updateConfig({
        matcher: {
          enabled: globalEnabled,
          auto_placement: autoPlacement,
          interval_minutes: intervalMinutes,
        },
      });
      toast.success("Global matcher settings saved");
    } catch (err) {
      toast.error(`Failed to save global settings: ${err}`);
    } finally {
      setSaving(false);
    }
  };

  const emptyRule: MatcherRule = {
    name: "",
    enabled: true,
    search_path: "",
    target_category: "",
    target_save_path: "",
    patterns: "*",
    priority: rules.length,
    require_same_extension: true,
    skip_unmatched: false,
    recheck: true,
  };

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin" />
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col p-4 gap-4 overflow-auto">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Matcher Configuration</h1>
        {health?.contract?.status !== "ok" && (
          <Badge variant="destructive" className="text-xs">
            Path contract: {health.contract.status}
          </Badge>
        )}
      </div>

      {/* Global Settings */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Global Matcher Settings</CardTitle>
          <CardDescription className="text-xs">
            Enable/disable the matcher and configure default behavior
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-2">
            <Checkbox
              id="global-enabled"
              checked={globalEnabled}
              onCheckedChange={(v) => setGlobalEnabled(v as boolean)}
            />
            <label htmlFor="global-enabled" className="text-sm">
              Matcher enabled
            </label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id="auto-placement"
              checked={autoPlacement}
              onCheckedChange={(v) => setAutoPlacement(v as boolean)}
            />
            <label htmlFor="auto-placement" className="text-sm">
              Auto placement enabled
            </label>
          </div>
          <div className="flex items-center gap-2">
            <label htmlFor="interval" className="text-sm w-20">
              Scan interval (minutes)
            </label>
            <Input
              id="interval"
              type="number"
              min="1"
              max="1440"
              value={intervalMinutes}
              onChange={(e) => setIntervalMinutes(Number(e.target.value))}
              className="w-24 text-sm"
            />
          </div>
          <Button size="sm" onClick={saveGlobalSettings} disabled={saving}>
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
            Save Global Settings
          </Button>
        </CardContent>
      </Card>

      {/* Rules List */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle className="text-sm">Matching Rules</CardTitle>
            <CardDescription className="text-xs">
              Define dynamic path-based matching rules. Rules are processed in priority order.
            </CardDescription>
          </div>
          <Button size="sm" onClick={() => setEditingRule(emptyRule)}>
            <Plus className="h-4 w-4 mr-1" />
            Add Rule
          </Button>
        </CardHeader>
        <CardContent>
          {rules.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-4">
              No rules defined. Add a rule to enable dynamic path matching.
            </p>
          ) : (
            <div className="space-y-2">
              {rules.map((rule) => (
                <div
                  key={rule.name}
                  className="flex items-center gap-2 p-2 border rounded-md hover:bg-muted/50"
                >
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-sm truncate">{rule.name}</div>
                    <div className="text-xs text-muted-foreground truncate">
                      {rule.search_path}
                    </div>
                  </div>
                  <Badge variant={rule.enabled ? "default" : "secondary"} className="text-xs">
                    {rule.enabled ? "Enabled" : "Disabled"}
                  </Badge>
                  <Badge variant="outline" className="text-xs">
                    Priority: {rule.priority}
                  </Badge>
                  <Button
                    size="icon"
                    variant="ghost"
                    className="h-8 w-8"
                    onClick={() => setEditingRule(rule)}
                  >
                    <Edit2 className="h-4 w-4" />
                  </Button>
                  <Button
                    size="icon"
                    variant="ghost"
                    className="h-8 w-8 text-destructive"
                    onClick={() => deleteRule(rule.name)}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* QBitTorrent Info */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">qBittorrent Reference</CardTitle>
          <CardDescription className="text-xs">
            Available categories and save paths from your qBittorrent instance
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <h3 className="text-sm font-medium mb-1">Categories</h3>
            {categories.length > 0 ? (
              <div className="flex flex-wrap gap-1">
                {categories.map((cat) => (
                  <Badge key={cat} variant="outline" className="text-xs">
                    {cat}
                  </Badge>
                ))}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">No categories defined</p>
            )}
          </div>
          <div>
            <h3 className="text-sm font-medium mb-1">Save Paths</h3>
            {savePaths.length > 0 ? (
              <div className="flex flex-wrap gap-1">
                {savePaths.map((path) => (
                  <Badge key={path} variant="outline" className="text-xs font-mono truncate max-w-xs">
                    {path}
                  </Badge>
                ))}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">No save paths found</p>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Rule Editor Dialog */}
      {editingRule && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center p-4 z-50">
          <Card className="w-full max-w-lg max-h-[80vh] overflow-auto">
            <CardHeader className="flex flex-row items-center justify-between">
              <CardTitle className="text-sm">
                {editingRule.name ? `Edit Rule: ${editingRule.name}` : "Add New Rule"}
              </CardTitle>
              <Button
                size="icon"
                variant="ghost"
                onClick={() => setEditingRule(null)}
              >
                <X className="h-4 w-4" />
              </Button>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <label className="text-sm font-medium">Rule Name *</label>
                <Input
                  value={editingRule.name}
                  onChange={(e) =>
                    setEditingRule({ ...editingRule, name: e.target.value })
                  }
                  placeholder="e.g., Movies Library"
                  className="text-sm"
                />
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium">Search Path *</label>
                <Input
                  value={editingRule.search_path}
                  onChange={(e) =>
                    setEditingRule({ ...editingRule, search_path: e.target.value })
                  }
                  placeholder="e.g., /data/media/movies"
                  className="text-sm font-mono"
                />
                <p className="text-xs text-muted-foreground">
                  Path to search for files. Supports glob patterns like /data/media/*
                </p>
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium">Target Category</label>
                <select
                  value={editingRule.target_category}
                  onChange={(e) =>
                    setEditingRule({ ...editingRule, target_category: e.target.value })
                  }
                  className="w-full h-8 rounded-md border border-input bg-transparent px-2 text-sm"
                >
                  <option value="">All categories</option>
                  {categories.map((cat) => (
                    <option key={cat} value={cat}>{cat}</option>
                  ))}
                </select>
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium">Target Save Path</label>
                <select
                  value={editingRule.target_save_path}
                  onChange={(e) =>
                    setEditingRule({ ...editingRule, target_save_path: e.target.value })
                  }
                  className="w-full h-8 rounded-md border border-input bg-transparent px-2 text-sm font-mono"
                >
                  <option value="">Any save path</option>
                  {savePaths.map((path) => (
                    <option key={path} value={path}>{path}</option>
                  ))}
                </select>
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium">File Patterns</label>
                <Input
                  value={editingRule.patterns}
                  onChange={(e) =>
                    setEditingRule({ ...editingRule, patterns: e.target.value })
                  }
                  placeholder="e.g., *.mkv,*.mp4"
                  className="text-sm font-mono"
                />
                <p className="text-xs text-muted-foreground">
                  Comma-separated glob patterns. Default: *
                </p>
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium">Priority</label>
                <Input
                  type="number"
                  value={editingRule.priority}
                  onChange={(e) =>
                    setEditingRule({
                      ...editingRule,
                      priority: Number(e.target.value),
                    })
                  }
                  placeholder="0"
                  className="text-sm w-24"
                />
                <p className="text-xs text-muted-foreground">
                  Lower number = higher priority
                </p>
              </div>

              <div className="flex items-center gap-2">
                <Checkbox
                  id="rule-enabled"
                  checked={editingRule.enabled}
                  onCheckedChange={(v) =>
                    setEditingRule({ ...editingRule, enabled: v as boolean })
                  }
                />
                <label htmlFor="rule-enabled" className="text-sm">
                  Rule enabled
                </label>
              </div>

              <div className="flex items-center gap-2">
                <Checkbox
                  id="rule-same-ext"
                  checked={editingRule.require_same_extension}
                  onCheckedChange={(v) =>
                    setEditingRule({ ...editingRule, require_same_extension: v as boolean })
                  }
                />
                <label htmlFor="rule-same-ext" className="text-sm">
                  Require same file extension
                </label>
              </div>

              <div className="flex items-center gap-2">
                <Checkbox
                  id="rule-skip-unmatched"
                  checked={editingRule.skip_unmatched}
                  onCheckedChange={(v) =>
                    setEditingRule({ ...editingRule, skip_unmatched: v as boolean })
                  }
                />
                <label htmlFor="rule-skip-unmatched" className="text-sm">
                  Skip unmatched files
                </label>
              </div>

              <div className="flex items-center gap-2">
                <Checkbox
                  id="rule-recheck"
                  checked={editingRule.recheck}
                  onCheckedChange={(v) =>
                    setEditingRule({ ...editingRule, recheck: v as boolean })
                  }
                />
                <label htmlFor="rule-recheck" className="text-sm">
                  Recheck after matching
                </label>
              </div>

              <div className="flex gap-2 pt-4">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setEditingRule(null)}
                >
                  Cancel
                </Button>
                <Button
                  size="sm"
                  onClick={() => saveRule(editingRule)}
                  disabled={saving}
                >
                  {saving ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin mr-1" />
                      Saving...
                    </>
                  ) : (
                    <>
                      <Check className="h-4 w-4 mr-1" />
                      Save Rule
                    </>
                  )}
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
