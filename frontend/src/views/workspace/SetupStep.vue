<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { useBookStore } from '@/stores/book'
import { useMetaStore } from '@/stores/meta'

const bookStore = useBookStore()
const meta = useMetaStore()

const IMAGE_SERVICES = [
  { value: 'doubao', label: '豆包' },
  { value: 'tongyi', label: '通义万相' },
] as const

/** 后端判定服务不可用时给出原因；拿不到健康信息时不拦，交给后端报错。 */
function serviceStatus(service: string) {
  return meta.health?.imageServices?.[service] ?? { available: true, reason: '' }
}

const unavailableReason = computed(() => {
  if (!meta.health) return ''
  const reasons = IMAGE_SERVICES.filter((s) => !serviceStatus(s.value).available).map(
    (s) => serviceStatus(s.value).reason,
  )
  return reasons.join(' ')
})

const form = reactive({
  title: '',
  author: '',
  protagonist: '',
  characterDescription: '',
  style: '',
  trimSizeId: '',
  pageCount: 8,
  imageService: 'doubao' as 'doubao' | 'tongyi',
})

// 用序列化快照做脏检查：字段少、结构扁平，比逐字段比对省事且不会漏。
const snapshot = ref('')
const saving = ref(false)

function syncFromBook() {
  const book = bookStore.book
  if (!book) return
  Object.assign(form, {
    title: book.title,
    author: book.author,
    protagonist: book.protagonist,
    characterDescription: book.characterDescription,
    style: book.style,
    trimSizeId: book.trimSize.id,
    pageCount: book.pageCount,
    imageService: book.imageService,
  })
  snapshot.value = JSON.stringify(form)
}

watch(() => bookStore.book?.id, syncFromBook, { immediate: true })

const dirty = computed(() => JSON.stringify(form) !== snapshot.value)

async function save() {
  if (!form.title.trim()) {
    ElMessage.warning('标题不能为空')
    return
  }
  saving.value = true
  try {
    await bookStore.patchBook({ ...form, title: form.title.trim() })
    snapshot.value = JSON.stringify(form)
    ElMessage.success('已保存')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="setup">
    <el-card shadow="never">
      <template #header>
        <span>绘本设置</span>
      </template>

      <el-form label-width="110px" label-position="left">
        <el-form-item label="标题" required>
          <el-input v-model="form.title" maxlength="60" />
        </el-form-item>

        <el-form-item label="作者">
          <el-input v-model="form.author" maxlength="30" placeholder="选填" />
        </el-form-item>

        <el-form-item label="主角名字">
          <el-input v-model="form.protagonist" maxlength="30" placeholder="例如：小兔子雪球" />
        </el-form-item>

        <el-form-item label="主角外貌">
          <el-input
            v-model="form.characterDescription"
            type="textarea"
            :rows="2"
            placeholder="例如：一只围着红围巾的小白兔，耳朵上有一小块灰斑"
          />
          <div class="tip">
            会原样追加到每一页的生图提示词里，用来保持各页主角长相一致。
            用中文填写即可 —— 提示词整条都是中文。
          </div>
        </el-form-item>

        <el-form-item label="画风">
          <el-select v-model="form.style" style="width: 260px">
            <el-option v-for="s in meta.styles" :key="s.key" :label="s.label" :value="s.key" />
          </el-select>
        </el-form-item>

        <el-form-item label="开本">
          <el-select v-model="form.trimSizeId" style="width: 260px">
            <el-option v-for="t in meta.trimSizes" :key="t.id" :label="t.label" :value="t.id" />
          </el-select>
        </el-form-item>

        <el-form-item label="目标页数">
          <el-input-number v-model="form.pageCount" :min="1" :max="30" />
          <div class="tip">
            分页时使用的页数。实际页数由故事长度决定，可能在「故事」步骤里被调整。
          </div>
        </el-form-item>

        <el-form-item label="生图服务">
          <el-radio-group v-model="form.imageService">
            <el-radio
              v-for="service in IMAGE_SERVICES"
              :key="service.value"
              :value="service.value"
              :disabled="!serviceStatus(service.value).available"
            >
              {{ service.label }}
              <span v-if="!serviceStatus(service.value).available" class="unavailable">
                不可用
              </span>
            </el-radio>
          </el-radio-group>
          <div v-if="unavailableReason" class="tip warn">{{ unavailableReason }}</div>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="saving" :disabled="!dirty" @click="save">
            保存设置
          </el-button>
          <el-button :disabled="!dirty" @click="syncFromBook">放弃修改</el-button>
          <span v-if="dirty" class="dirty">有未保存的修改</span>
        </el-form-item>
      </el-form>
    </el-card>

    <el-alert
      v-if="meta.health && !meta.health.keysConfigured.doubao && !meta.health.keysConfigured.text"
      type="warning"
      :closable="false"
      show-icon
      class="alert"
      title="尚未配置任何 API key，生图功能不可用。"
      description="在 backend/.env 里填 ARK_API_KEY（豆包）或 API_KEY（文本模型），参考 backend/.env.example。"
    />
  </div>
</template>

<style scoped>
.setup {
  max-width: 720px;
}

.tip {
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
  margin-top: 4px;
}

.tip.warn {
  color: #e6a23c;
}

.unavailable {
  font-size: 11px;
  color: #c0c4cc;
  margin-left: 2px;
}

.dirty {
  font-size: 12px;
  color: #e6a23c;
  margin-left: 12px;
}

.alert {
  margin-top: 16px;
}
</style>
