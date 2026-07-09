import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { UploadCloud, Loader2, Trash2, Film, AlertCircle } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { ApiError, deleteEventMedia, fetchEventMedia, uploadEventMedia } from "@/lib/api";
import type { EventMediaItem } from "@/lib/types";

// Раздел «Медиа с ивентов»: загрузка фото/видео (видео сжимается на сервере под лимит
// Wazzup 16 МБ; если не влезло — сервер вернёт понятную ошибку), просмотр, удаление.
// Бот шлёт случайные из этих медиа лидам (приглашение / «как выглядит» / сомнение).

const KEY = ["event-media"];
const MB = 1024 * 1024;
// Клиентский предупредительный кап — до загрузки (сервер всё равно проверит/сожмёт).
const CLIENT_MAX = 200 * MB;

export function EventMediaSection() {
  const qc = useQueryClient();
  const { data: media = [], isPending } = useQuery({ queryKey: KEY, queryFn: fetchEventMedia });
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const uploadM = useMutation({
    mutationFn: (file: File) => uploadEventMedia(file),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
    onError: (e) => setError(e instanceof ApiError ? e.message : "No se pudo subir el archivo"),
  });

  const delM = useMutation({
    mutationFn: (id: number) => deleteEventMedia(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  });

  const pick = (files: FileList | null) => {
    setError(null);
    if (!files) return;
    for (const f of Array.from(files)) {
      if (f.size > CLIENT_MAX) {
        setError(`«${f.name}» es demasiado grande (máx ${CLIENT_MAX / MB} MB).`);
        continue;
      }
      uploadM.mutate(f);
    }
    if (fileRef.current) fileRef.current.value = "";
  };

  return (
    <Card className="space-y-3 p-4">
      <div>
        <div className="text-sm font-medium text-ink">Медиа с ивентов</div>
        <p className="text-[11px] text-muted">
          Бот присылает эти фото/видео лидам. Видео сжимается автоматически (лимит 16 МБ).
        </p>
      </div>

      <input ref={fileRef} type="file" accept="image/*,video/*" multiple className="hidden"
        onChange={(e) => pick(e.target.files)} />
      <button
        type="button"
        onClick={() => fileRef.current?.click()}
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => { e.preventDefault(); pick(e.dataTransfer.files); }}
        disabled={uploadM.isPending}
        className="flex w-full flex-col items-center justify-center gap-1.5 rounded-control border border-dashed border-line py-5 text-sm text-muted transition-colors hover:border-primary/50 hover:text-primary disabled:opacity-60"
      >
        {uploadM.isPending
          ? <><Loader2 size={20} className="animate-spin" /> Загрузка… (видео сжимается, подождите)</>
          : <><UploadCloud size={20} /> Перетащите фото/видео или нажмите, чтобы выбрать</>}
      </button>

      {error && (
        <p className="flex items-start gap-1 text-xs text-danger">
          <AlertCircle size={13} className="mt-0.5 shrink-0" /> {error}
        </p>
      )}

      {isPending ? (
        <div className="text-xs text-muted">Загрузка…</div>
      ) : media.length === 0 ? (
        <div className="text-xs text-muted">Пока ничего не загружено.</div>
      ) : (
        <div className="grid grid-cols-3 gap-2">
          {media.map((m) => <MediaTile key={m.id} item={m} onDelete={() => delM.mutate(m.id)} />)}
        </div>
      )}
    </Card>
  );
}

function MediaTile({ item, onDelete }: { item: EventMediaItem; onDelete: () => void }) {
  const sizeMb = item.sizeBytes ? (item.sizeBytes / MB).toFixed(1) : null;
  return (
    <div className="relative overflow-hidden rounded-control border border-line bg-surface">
      {item.mediaType === "video" ? (
        <video src={item.url} className="h-24 w-full object-cover" preload="metadata" muted />
      ) : (
        <img src={item.url} alt="" className="h-24 w-full object-cover" />
      )}
      {item.mediaType === "video" && (
        <span className="absolute left-1 top-1 flex items-center gap-0.5 rounded bg-black/60 px-1 py-0.5 text-[9px] font-medium text-white">
          <Film size={9} /> {sizeMb ? `${sizeMb}МБ` : "video"}
        </span>
      )}
      <button
        type="button"
        onClick={onDelete}
        aria-label="Удалить"
        className="absolute right-1 top-1 flex h-6 w-6 items-center justify-center rounded-full bg-surface/90 text-danger shadow-soft"
      >
        <Trash2 size={13} />
      </button>
    </div>
  );
}
