import time

import numpy
from PIL import Image
import os
from moviepy import VideoFileClip
from img2pdf import convert
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import cv2
import pytesseract
from pdf2image import convert_from_path
from pdf2docx import Converter
import win32com.client
from pathlib import Path




def convert_images_to_png(source_dir, target_dir, select):
    # 确保目标目录存在，如果不存在则创建
    if not os.path.exists(source_dir):
        return
    if not os.path.exists(target_dir):
        os.makedirs(target_dir)
    time.sleep(0.1)
    # 遍历源目录中的所有文件
    for filename in os.listdir(source_dir):
        # 构建源文件的完整路径
        source_path = os.path.join(source_dir, filename)

        # 确保是一个文件而非目录
        if os.path.isfile(source_path):
            # 检查文件是否为图片（这里以.jpg、.jpeg、.png为例）
            if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp',)):
                # 尝试打开图片
                try:
                    with Image.open(source_path) as img:
                        # 构建目标文件的完整路径和文件名（将原文件扩展名改为.png）
                        base, ext = os.path.splitext(filename)
                        target_path = os.path.join(target_dir, base + '.png')

                        # 保存转换后的图片
                        img.save(target_path, "PNG")
                        print(f"Converted {source_path} to {target_path}")
                        if select.lower() == 'n':
                            os.remove(source_path)
                except IOError:
                    print(f"Error converting {source_path}")


def convert_video_to_gif(input_folder_path, output_folder_path, select):
    # 如果输出文件夹不存在，则创建它
    if not os.path.exists(output_folder_path):
        os.makedirs(output_folder_path)

    # 遍历输入文件夹中的所有文件
    for filename in os.listdir(input_folder_path):
        # 检查文件扩展名是否为.mp4
        if filename.lower().endswith('.mp4'):
            # 构建完整的文件路径
            input_file_path = os.path.join(input_folder_path, filename)
            # 提取文件名（不带扩展名）作为输出文件名
            output_filename = os.path.splitext(filename)[0] + '.gif'
            # 构建完整的输出文件路径
            output_file_path = os.path.join(output_folder_path, output_filename)

            # 加载视频文件
            clip = VideoFileClip(input_file_path)

            # 可选：设置GIF的持续时间（例如，这里设置为3秒）
            # clip = clip.subclip(0, 3)

            # 可选：设置GIF的帧率（例如，这里设置为10帧每秒）
            # clip.fps = 10
            # 注意：如果不设置fps，moviepy将使用视频的原始帧率，但可能会生成较大的GIF文件

            # 为了减小GIF文件大小，可以调整resize参数来降低分辨率
            # 例如，将分辨率设置为原始宽度和高度的50%
            # clip = clip.resize(newsize=(clip.size[0]//2, clip.size[1]//2))

            # 将视频文件写入GIF文件
            clip.write_gif(output_file_path, fps=clip.fps)  # 可以根据需要调整fps参数

            # 释放资源
            clip.close()
            print(f'Converted {input_file_path} to {output_file_path}')


def image_and_video_rename(source_dir, target_dir, select):
    # 确保目标目录存在，如果不存在则创建
    if not os.path.exists(source_dir):
        return
    if not os.path.exists(target_dir):
        os.makedirs(target_dir)
    time.sleep(0.1)
    # 遍历源目录中的所有文件
    n = -1
    for filename in os.listdir(source_dir):
        # 构建源文件的完整路径
        source_path = os.path.join(source_dir, filename)

        # 确保是一个文件而非目录
        if os.path.isfile(source_path):
            # 检查文件是否为目标格式（这里以.jpg、.jpeg、.png为例）
            if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mp3', '.1')):
                n += 1
                # 尝试打开文件
                try:
                    # 构建目标文件的完整路径和文件名
                    base, ext = os.path.splitext(filename)

                    base = n
                    target_path = os.path.join(target_dir, str(base) + ext)
                    print(target_path)
                    os.renames(source_path, target_path)
                    if os.path.exists(source_path):

                        continue
                    time.sleep(0.1)
                    print(f"Converted {source_path} to {target_path}")

                except IOError:
                    print(f"Error converting {source_path}")


def image_pdf(source_dir, target_dir, select, filename):
    # 获取图片文件夹中的所有图片文件
    image_files = [os.path.join(source_dir, f) for f in os.listdir(source_dir)
                   if f.endswith(('.png', '.jpg', '.jpeg'))]
    # 检查是否找到图片文件
    if not image_files:

        print("未找到图片文件。")
        return
    else:
        target_file = os.path.join(target_directory, filename + ".pdf")
        # 使用img2pdf将图片转换为PDF数据
        pdf_bytes = convert(image_files)

        # 将PDF数据写入文件
        with open(target_file, 'wb') as f:
            f.write(pdf_bytes)
        print(f"图片已成功合并到PDF文件：{target_dir}")


def simple_images_to_pdf_reportlab(image_folder, output_pdf_path):
    c = canvas.Canvas(output_pdf_path, pagesize=A4)
    width, height = A4
    image_files = sorted([f for f in os.listdir(image_folder) if f.lower().endswith(('png', 'jpg', 'jpeg'))])

    for i, image_file in enumerate(image_files):
        image_path = os.path.join(image_folder, image_file)
        with Image.open(image_path) as img:
            img_width, img_height = img.size
            aspect_ratio = min(width / img_width, height / img_height)
            new_width = img_width * aspect_ratio
            new_height = img_height * aspect_ratio
            x_offset = (width - new_width) / 2
            y_offset = (height - new_height) / 2

            if i > 0:  # 不是第一页时，先结束前一页
                c.showPage()

            c.drawImage(image_path, x_offset, y_offset - new_height, width=new_width, height=new_height)

    c.showPage()  # 结束最后一页
    c.save()


def pdf_to_word(pdf_path, word_path):
    cv = Converter(pdf_path)
    cv.convert(word_path)
    cv.close()


def safe_docx_to_pdf(docx_path, pdf_path):
    word = None
    doc = None
    try:
        # 确保路径是绝对路径
        docx_path = str(Path(docx_path).absolute())
        pdf_path = str(Path(pdf_path).absolute())

        # 启动Word进程（添加延迟避免启动过快）
        word = win32com.client.DispatchEx("Word.Application")  # DispatchEx避免复用现有进程
        time.sleep(0.5)

        # 打开文档（禁止自动更新、弹窗）
        doc = word.Documents.Open(
            FileName=docx_path,
            ReadOnly=True,
            AddToRecentFiles=False,
            Visible=False
        )
        time.sleep(0.5)

        # 保存为PDF
        doc.SaveAs(
            FileName=pdf_path,
            FileFormat=17  # 17对应PDF格式
        )
        time.sleep(0.5)

    except Exception as e:
        print(f"docx转pdf失败: {str(e)}")
    finally:
        # 优先关闭文档，再关闭Word进程
        if doc:
            try:
                doc.Close(SaveChanges=False)
                time.sleep(0.3)
            except Exception:
                pass
        if word:
            try:
                word.Quit(SaveChanges=False)
                time.sleep(0.3)
            except Exception:
                pass
        # 强制释放COM对象
        del doc
        del word

def ocr_pdf_to_text(pdf_path):
    images = convert_from_path(pdf_path)
    text = ""
    for image in images:
        image = cv2.cvtColor(numpy.array(image), cv2.COLOR_RGB2BGR)
        text += pytesseract.image_to_string(image)
    return text


source_directory = 'G:\\li\\分类'  # 当前目录下的source文件夹
target_directory = 'G:\\li\\分类'  # 当前目录下的target文件夹
# print("输入操作:1:转为pdf 2：重命名3：转为png 4：转为gif。可多选")
#
# selec = input()
# i = input("是否保留源文件:y or n")
# if selec == '1':
#     name = input("请输入名字：")
#     image_pdf(source_directory, target_directory, i, name)
# elif selec == '2':
#     image_and_video_rename(source_directory, target_directory, i)
# elif selec == '3':
#     convert_images_to_png(source_directory, target_directory, i)
# elif selec == '4':
#     convert_video_to_gif(source_directory, target_directory, i)
# elif len(selec) > 1:
#     name = input("请输入名字：")
#     if '2' in selec:
#         image_and_video_rename(source_directory, target_directory, i)
#
#     if '3' in selec:
#         convert_images_to_png(source_directory, target_directory, i)
#     if '4' in selec:
#         convert_video_to_gif(source_directory, target_directory, i)
#     if '1' in selec:
#         image_pdf(source_directory, target_directory, i)

#convert_images_to_png(source_directory, target_directory, 'n')
# image_pdf(source_directory, target_directory,1)
#convert_video_to_gif(source_directory, target_directory,1)
# image_and_video_rename(source_directory, target_directory, 1)
# 使用示例
# text = ocr_pdf_to_text("D:\\img_download\\杂图\\1.pdf")

# 使用示例
#pdf_to_word("D:\\img_download\\杂图\\1.pdf", "D:\\img_download\\杂图\\1.docx")

safe_docx_to_pdf("E:\\to\\0.docx","E:\\to\\10.pdf")
