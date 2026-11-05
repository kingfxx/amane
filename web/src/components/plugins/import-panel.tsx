import {
  Alert,
  Button,
  Checkbox,
  Group,
  Modal,
  ScrollArea,
  Select,
  Stack,
  Text,
} from "@mantine/core";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { listLibrariesOptions } from "@/client/@tanstack/react-query.gen";
import { executeImport, previewImport } from "@/client/sdk.gen";
import type { ImportPreviewResponse } from "@/client/types.gen";
import { PathPicker } from "@/components/path-picker";
import { extractErrorMessage } from "@/lib/api-error";

export function ImportPanel({
  sourceId,
  opened,
  onClose,
}: {
  sourceId: string;
  opened: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation("plugins");
  const [path, setPath] = useState("");
  const [recursive, setRecursive] = useState(false);
  const [library, setLibrary] = useState<string | null>(null);
  const [preview, setPreview] = useState<ImportPreviewResponse | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [submitted, setSubmitted] = useState<number | null>(null);
  const libraries = useQuery({ ...listLibrariesOptions(), enabled: opened });
  const inspect = useMutation({
    mutationFn: async () => {
      const result = await previewImport({
        body: { source_id: sourceId, path, recursive },
        throwOnError: true,
      });
      return result.data;
    },
    onSuccess: (data) => {
      if (!data) return;
      setPreview(data);
      setSelected(data.items.filter((item) => item.candidate != null).map((item) => item.path));
      setSubmitted(null);
    },
  });
  const execute = useMutation({
    mutationFn: async () => {
      if (!preview) throw new Error(t("import.previewRequired"));
      const result = await executeImport({
        body: {
          items: preview.items.flatMap((item) => {
            if (!item.candidate || !selected.includes(item.path)) return [];
            return [
              {
                source_id: sourceId,
                root: preview.root,
                source_path: item.candidate.source_path,
                source_hash: item.candidate.source_hash,
                library_id: library ? Number(library) : null,
              },
            ];
          }),
        },
        throwOnError: true,
      });
      return result.data;
    },
    onSuccess: (data) => {
      setSubmitted(data?.task_ids.length ?? 0);
      setSelected([]);
    },
  });
  const pending = inspect.isPending || execute.isPending;
  const reset = () => {
    setPreview(null);
    setSelected([]);
    setSubmitted(null);
    inspect.reset();
    execute.reset();
  };
  const error = inspect.error ?? execute.error;
  return (
    <Modal
      opened={opened}
      onClose={pending ? () => {} : onClose}
      title={t("import.title")}
      size="xl"
      centered
    >
      <Stack>
        <Text size="sm" c="dimmed">
          {t("import.hint")}
        </Text>
        <PathPicker
          value={path}
          onChange={(value) => {
            setPath(value);
            reset();
          }}
          pathType="directory"
          label={t("import.path")}
          disabled={pending}
        />
        <Checkbox
          checked={recursive}
          onChange={(event) => {
            setRecursive(event.currentTarget.checked);
            reset();
          }}
          label={t("import.recursive")}
          disabled={pending}
        />
        <Select
          clearable
          label={t("import.library")}
          description={t("import.libraryHint")}
          value={library}
          onChange={setLibrary}
          disabled={pending}
          data={(libraries.data?.items ?? []).map((item) => ({
            value: String(item.id),
            label: item.name,
          }))}
        />
        <Button
          onClick={() => {
            reset();
            inspect.mutate();
          }}
          loading={inspect.isPending}
          disabled={!path.trim() || execute.isPending}
        >
          {t("import.preview")}
        </Button>
        {error ? <Alert color="red">{extractErrorMessage(error, t("import.failed"))}</Alert> : null}
        {preview ? (
          <ScrollArea.Autosize mah={360}>
            <Stack gap="xs">
              {preview.items.length === 0 ? <Text>{t("import.empty")}</Text> : null}
              {preview.items.map((item) => (
                <Stack key={item.path} gap={2}>
                  <Checkbox
                    disabled={pending || !item.candidate}
                    checked={selected.includes(item.path)}
                    onChange={(event) => {
                      const checked = event.currentTarget.checked;
                      setSelected((values) =>
                        checked
                          ? [...values, item.path]
                          : values.filter((value) => value !== item.path),
                      );
                    }}
                    label={
                      item.candidate
                        ? `${item.candidate.metadata.number} · ${item.candidate.metadata.title ?? ""}`
                        : item.path
                    }
                  />
                  <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                    {item.path}
                  </Text>
                  {item.error ? (
                    <Text size="sm" c="red">
                      {item.error}
                    </Text>
                  ) : null}
                  {item.existing_metadata_id ? (
                    <Text size="xs" c="orange">
                      {t("import.fillOnly")}
                    </Text>
                  ) : null}
                  {item.candidate ? (
                    <>
                      {(item.candidate.warnings ?? []).map((warning) => (
                        <Text size="xs" c="orange" key={warning}>
                          {warning}
                        </Text>
                      ))}
                      <Text size="xs">
                        {t("import.counts", {
                          images: item.candidate.images?.length ?? 0,
                          files: item.candidate.media_paths?.length ?? 0,
                        })}
                      </Text>
                      {(item.candidate.metadata.trailer_urls ?? []).map((url) => (
                        <Text size="xs" key={url} style={{ overflowWrap: "anywhere" }}>
                          {t("import.remoteTrailer", { path: url })}
                        </Text>
                      ))}
                      {(item.candidate.local_trailer_paths ?? []).map((local) => (
                        <Text size="xs" key={local} style={{ overflowWrap: "anywhere" }}>
                          {t("import.localTrailer", { path: local })}
                        </Text>
                      ))}
                    </>
                  ) : null}
                </Stack>
              ))}
            </Stack>
          </ScrollArea.Autosize>
        ) : null}
        {submitted != null ? (
          <Alert color="teal">{t("import.submitted", { count: submitted })}</Alert>
        ) : null}
        <Group justify="flex-end">
          <Button
            onClick={() => execute.mutate()}
            loading={execute.isPending}
            disabled={!preview || selected.length === 0 || inspect.isPending}
          >
            {t("import.execute", { count: selected.length })}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
